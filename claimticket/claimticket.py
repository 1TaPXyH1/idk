# Credits and orignal author: https://github.com/fourjr/modmail-plugins/blob/master/claim/claim.py
# Slightly modified for Minion_Kadin#2022 (discord)
# Please use the original plugin as this one may cause your bot to nuke the world

import discord
from discord.ext import commands
import time
import asyncio
from datetime import datetime, timedelta
from discord.ext.commands import CooldownMapping, BucketType
import random
import os
from dotenv import load_dotenv
import pandas as pd
import aiohttp
import motor.motor_asyncio
from pymongo import UpdateOne
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

try:
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

load_dotenv()

from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id


@commands.check
async def is_in_thread(ctx):
    """
    Check if the command is being used in a thread channel
    
    Args:
        ctx: The command context
    
    Returns:
        bool: True if in a thread, False otherwise
    """
    # Check if the channel is a thread
    if not isinstance(ctx.channel, discord.Thread):
        raise commands.CheckFailure("This command can only be used in a thread.")
    return True


class ClaimThread(commands.Cog):
    """Allows supporters to claim thread by sending claim in the thread channel"""
    async def initialize_mongodb(self):
        """
        Initialize MongoDB connection and collections
        """
        try:
            # Create Motor client for direct MongoDB access with robust connection settings
            self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                self.mongo_uri, 
                serverSelectionTimeoutMS=30000,  # 30-second server selection timeout
                connectTimeoutMS=30000,          # 30-second connection timeout
                socketTimeoutMS=30000,           # 30-second socket timeout
                maxPoolSize=10,                  # Connection pool size
                minPoolSize=1,                   # Minimum connections in pool
                retryWrites=True,                # Retry write operations
                appName='ModmailTicketPlugin'    # Descriptive app name
            )
            
            # Select database
            self.mongo_db = self.mongo_client[self.mongo_db_name]
            
            # Initialize specific collections
            self.ticket_stats_collection = self.mongo_db['ticket_stats']
            self.config_collection = self.mongo_db['plugin_configs']
            
            # Create collections with validation
            collection_names = await self.mongo_db.list_collection_names()
            collections_to_create = {
                'ticket_stats': {'description': 'Ticket Closure Statistics'},
                'plugin_configs': {'description': 'Plugin Configurations'}
            }
                    
            for collection_name, collection_info in collections_to_create.items():
                if collection_name not in collection_names:
                    try:
                        await self.mongo_db.create_collection(collection_name)
                        print(f"✅ Created {collection_info['description']} Collection: {collection_name}")
                    except Exception as create_error:
                        print(f"❌ Error creating {collection_info['description']} Collection: {create_error}")
                else:
                    print(f"ℹ️ {collection_info['description']} Collection already exists: {collection_name}")
                        
            print("✅ Verified Collections")
            return True
        
        except Exception as e:
            print(f"🚨 MongoDB Initialization Error: {e}")
            # Fallback to in-memory collections
            self.ticket_stats_collection = {}
            self.config_collection = {}
            return False

    def __init__(self, bot):
        self.bot = bot
        
        # Reintroduce check_message_cache with minimal implementation
        self.check_message_cache = {}
        
        # Comprehensive environment variable logging
        print("🔍 Initializing MongoDB Connection")
        print("Environment Variables:")
        mongodb_env_vars = [
            'MONGODB_URI', 'MONGODB_DATABASE', 'MONGODB_USERNAME', 
            'MONGODB_PASSWORD', 'MONGODB_CLUSTER_URL', 'MONGODB_OPTIONS'
        ]
        for key in mongodb_env_vars:
            print(f"  {key}: {os.getenv(key, 'Not Set')}")
        
        # Hardcoded fallback values
        FALLBACK_MONGODB_USERNAME = '111iotapxrb'
        FALLBACK_MONGODB_PASSWORD = 'fEJdHM55QIYPVBDb'
        FALLBACK_MONGODB_CLUSTER_URL = 'tickets.eqqut.mongodb.net'
        FALLBACK_MONGODB_OPTIONS = 'retryWrites=true&w=majority&appName=Tickets'
        FALLBACK_MONGODB_DATABASE = 'Tickets'
        
        # Prioritize connection strategies with more robust fallback
        def get_env_with_fallback(primary_key, fallback_value):
            value = os.getenv(primary_key, fallback_value)
            print(f"🌐 Selected {primary_key}: {value}")
            return value
        
        # Construct MongoDB URI dynamically
        mongodb_username = get_env_with_fallback('MONGODB_USERNAME', FALLBACK_MONGODB_USERNAME)
        mongodb_password = get_env_with_fallback('MONGODB_PASSWORD', FALLBACK_MONGODB_PASSWORD)
        mongodb_cluster_url = get_env_with_fallback('MONGODB_CLUSTER_URL', FALLBACK_MONGODB_CLUSTER_URL)
        mongodb_options = get_env_with_fallback('MONGODB_OPTIONS', FALLBACK_MONGODB_OPTIONS)
        
        # Fallback to direct URI if not constructed from components
        self.mongo_uri = (
            get_env_with_fallback('MONGODB_URI', 
                f"mongodb+srv://{mongodb_username}:{mongodb_password}@{mongodb_cluster_url}/?{mongodb_options}"
            )
        )
        
        self.mongo_db_name = get_env_with_fallback(
            'MONGODB_DATABASE', 
            FALLBACK_MONGODB_DATABASE
        )
        
        # Validate MongoDB URI
        try:
            from urllib.parse import urlparse
            parsed_uri = urlparse(self.mongo_uri)
            print(f"🌐 Parsed MongoDB URI:")
            print(f"  Scheme: {parsed_uri.scheme}")
            print(f"  Hostname: {parsed_uri.hostname}")
            print(f"  Path: {parsed_uri.path}")
            
            # Additional validation
            if not parsed_uri.hostname:
                raise ValueError("Invalid MongoDB hostname")
        except Exception as uri_parse_error:
            print(f"❌ URI Parsing Error: {uri_parse_error}")
            # Force fallback URI if parsing fails
            self.mongo_uri = f"mongodb+srv://{FALLBACK_MONGODB_USERNAME}:{FALLBACK_MONGODB_PASSWORD}@{FALLBACK_MONGODB_CLUSTER_URL}/?{FALLBACK_MONGODB_OPTIONS}"
            print(f"⚠️ Forced Fallback URI: {self.mongo_uri}")
        
        # Schedule MongoDB initialization
        self.bot.loop.create_task(self.initialize_mongodb())
        
        # Initialize necessary attributes
        self.default_config = {
            'claim_limit': 5,  # Default claim limit
            'override_roles': []  # Default override roles
        }
        
        # Ticket export webhook (optional)
        self.ticket_export_webhook = None

    async def clean_old_claims(self):
        """Clean up claims for non-existent channels"""
        cursor = self.ticket_claims_collection.find({'guild_id': str(self.bot.modmail_guild.id)})
        async for doc in cursor:
            if 'thread_id' in doc:
                channel = self.bot.get_channel(int(doc['thread_id']))  # Use cache first
                if not channel and ('status' not in doc or doc['status'] != 'closed'):
                    await self.ticket_claims_collection.find_one_and_update(
                        {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                        {'$set': {'status': 'closed'}}
                    )

    @commands.Cog.listener()
    async def on_ready(self):
        """Clean up old claims when bot starts"""
        await self.clean_old_claims()

    async def get_config(self):
        """
        Retrieve plugin configuration
        
        :return: Configuration dictionary
        """
        config = await self.config_collection.find_one({'_id': 'claim_config'}) or {}
        return {
            'claim_limit': config.get('claim_limit', self.default_config['claim_limit']),
            'override_roles': config.get('override_roles', self.default_config['override_roles'])
        }

    async def handle_rate_limit(self, ctx):
        """Handle rate limits with better caching"""
        channel_id = str(ctx.channel.id)
        now = time.time()
        
        # Initialize or clean up usage tracking
        if channel_id not in self.command_usage:
            self.command_usage[channel_id] = 0
            self.reset_times[channel_id] = now + 2
            
        # Clean old entries
        if now > self.reset_times[channel_id]:
            self.command_usage[channel_id] = 0
            self.reset_times[channel_id] = now + 2
            
        # Check usage
        self.command_usage[channel_id] += 1
        if self.command_usage[channel_id] > 2:  # More than 2 commands in 2 seconds
            wait_time = 600  # 10 minutes
            await ctx.message.add_reaction('⏳')
            await ctx.send(
                f"Rate limit exceeded. Please wait {wait_time//60} minutes before trying again.",
                delete_after=10
            )
            await asyncio.sleep(wait_time)
            try:
                await ctx.message.remove_reaction('⏳', self.bot.user)
            except:
                pass
            
            # Reset after waiting
            self.command_usage[channel_id] = 0
            self.reset_times[channel_id] = now + 2
            return False
            
        return True

    async def get_channel(self, channel_id: int):
        """Get channel with optimized caching"""
        if channel_id in self.channel_cache:
            channel, timestamp = self.channel_cache[channel_id]
            if time.time() - timestamp < self.cache_lifetime:
                return channel

        channel = self.bot.get_channel(channel_id)
        if channel:
            self.channel_cache[channel_id] = (channel, time.time())
            return channel

        return None  # Don't fetch if not in cache

    async def get_user(self, user_id: int):
        """Get user with optimized caching"""
        if user_id in self.user_cache:
            user, timestamp = self.user_cache[user_id]
            if time.time() - timestamp < self.cache_lifetime:
                return user

        user = self.bot.get_user(user_id)
        if user:
            self.user_cache[user_id] = (user, time.time())
            return user

        return None  # Don't fetch if not in cache

    @commands.command(name="lb")
    async def claim_leaderboard(self, ctx, days: int = None):
        """Show claim leaderboard
        
        Usage:
        !lb [days] - Show leaderboard for specified days (optional)
        """
        claims = {}
        total_claims = {}
        
        # Calculate cutoff date if days specified
        cutoff_date = None
        if days:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        async for doc in self.ticket_claims_collection.find(
            {
                'guild_id': str(self.bot.modmail_guild.id),
                'claimers': {'$exists': True}
            }
        ):
            # Skip if before cutoff date
            if cutoff_date and 'claimed_at' in doc:
                claimed_at = datetime.fromisoformat(doc['claimed_at'])
                if claimed_at < cutoff_date:
                    continue
                    
            for claimer in doc['claimers']:
                total_claims[claimer] = total_claims.get(claimer, 0) + 1
                # Check if claim is active
                try:
                    channel = self.bot.get_channel(int(doc['thread_id']))
                    if channel and ('status' not in doc or doc['status'] != 'closed'):
                        claims[claimer] = claims.get(claimer, 0) + 1
                    elif not channel and ('status' not in doc or doc['status'] != 'closed'):
                        # Update status if channel doesn't exist
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )
                except:
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )

        if not total_claims:
            embed = discord.Embed(
                title="Claims Leaderboard",
                description="No claims found",
                color=self.bot.main_color
            )
            return await ctx.send(embed=embed)

        sorted_claims = sorted(total_claims.items(), key=lambda x: x[1], reverse=True)[:10]
        
        description = []
        for i, (user_id, claim_count) in enumerate(sorted_claims, 1):
            user = await self.get_user(int(user_id))
            name = user.name if user else f"User {user_id}"
            active = claims.get(user_id, 0)
            
            claim_text = "claim" if claim_count == 1 else "claims"
            description.append(
                f"{i}.  {name}     |  {claim_count} {claim_text}  |  {active} active"
            )

        embed = discord.Embed(
            title="Claims Leaderboard",
            description="\n".join(description),
            color=self.bot.main_color
        )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def thread_claim(self, ctx):
        """Claim a thread without renaming"""
        thread = await self.ticket_claims_collection.find_one({'thread_id': str(ctx.thread.channel.id), 'guild_id': str(self.bot.modmail_guild.id)})
        has_active_claimers = thread and thread.get('claimers') and len(thread['claimers']) > 0
        
        if not has_active_claimers:
            try:
                # Only handle subscription
                if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                    self.bot.config["subscriptions"][str(ctx.thread.id)] = []
                if ctx.author.mention not in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                    self.bot.config["subscriptions"][str(ctx.thread.id)].append(ctx.author.mention)
                    await self.bot.config.update()

                # Update database without renaming
                if thread is None:
                    await self.ticket_claims_collection.insert_one({
                        'guild_id': str(self.bot.modmail_guild.id),
                        'thread_id': str(ctx.thread.channel.id),
                        'claimers': [str(ctx.author.id)],
                        'status': 'open'
                    })
                else:
                    await self.ticket_claims_collection.find_one_and_update(
                        {'thread_id': str(ctx.thread.channel.id), 'guild_id': str(self.bot.modmail_guild.id)},
                        {'$set': {'claimers': [str(ctx.author.id)], 'status': 'open'}}
                    )

                embed = discord.Embed(
                    color=self.bot.main_color,
                    description=f"Successfully claimed the thread.\n{ctx.author.mention} is now subscribed to this thread."
                )
                await ctx.send(embed=embed)
            except:
                await ctx.message.add_reaction('❌')
        else:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def thread_unclaim(self, ctx):
        """Unclaim a thread without renaming"""
        thread = await self.ticket_claims_collection.find_one({'thread_id': str(ctx.thread.channel.id), 'guild_id': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            try:
                await self.ticket_claims_collection.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild_id': str(self.bot.modmail_guild.id)}, 
                    {'$pull': {'claimers': str(ctx.author.id)}}
                )
                
                # Only handle unsubscription
                if ctx.author.mention in self.bot.config["subscriptions"].get(str(ctx.thread.id), []):
                    self.bot.config["subscriptions"][str(ctx.thread.id)].remove(ctx.author.mention)
                    await self.bot.config.update()
                
                embed = discord.Embed(
                    color=self.bot.main_color,
                    description=f"Removed from claimers.\n{ctx.author.mention} is now unsubscribed from this thread."
                )
                await ctx.send(embed=embed)
            except:
                await ctx.message.add_reaction('❌')
        else:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def rename(self, ctx, *, new_name: str):
        """Rename the thread channel (optional command)"""
        try:
            await ctx.thread.channel.edit(name=new_name)
            await ctx.message.add_reaction('✅')
        except:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def thread_claims(self, ctx):
        """Check which channels you have claimed"""
        cursor = self.ticket_claims_collection.find({'guild_id':str(self.bot.modmail_guild.id)})
        active_channels = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(ctx.author.id) in doc['claimers']:
                if 'status' not in doc or doc['status'] != 'closed':
                    try:
                        channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                        if channel:
                            active_channels.append(channel)
                        else:
                            await self.ticket_claims_collection.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                                {'$set': {'status': 'closed'}}
                            )
                    except discord.NotFound:
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )

        embed = discord.Embed(title='Your claimed tickets:', color=self.bot.main_color)
        if active_channels:
            description = []
            for ch in active_channels:
                try:
                    recipient_id = match_user_id(ch.topic)
                    if recipient_id:
                        recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)
                        description.append(f"{ch.mention} - {recipient.name if recipient else 'Unknown User'}")
                    else:
                        description.append(ch.mention)
                except:
                    description.append(ch.mention)
            embed.description = "\n".join(description)
        else:
            embed.description = "No active claims"
            
        try:
            await ctx.send(embed=embed)
        except:
            try:
                if active_channels:
                    await ctx.send("Your claimed tickets:\n" + "\n".join([ch.mention for ch in active_channels]))
                else:
                    await ctx.send("No active claims")
            except:
                pass

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="stats")
    async def claim_stats(self, ctx, member: discord.Member = None):
        """View comprehensive claim statistics"""
        target = member or ctx.author
        
        # Get all claims for this user
        cursor = self.ticket_claims_collection.find({'guild_id': str(self.bot.modmail_guild.id)})
        active_claims = []
        closed_claims = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(target.id) in doc['claimers']:
                try:
                    channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                    if channel and ('status' not in doc or doc['status'] != 'closed'):
                        active_claims.append(doc)
                    else:
                        closed_claims.append(doc)
                        if not channel and ('status' not in doc or doc['status'] != 'closed'):
                            await self.ticket_claims_collection.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                                {'$set': {'status': 'closed'}}
                            )
                except (discord.NotFound, discord.Forbidden):
                    closed_claims.append(doc)
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )
        
        total_claims = len(active_claims) + len(closed_claims)
        
        # Get claim limit
        config = await self.get_config()
        limit = config.get('claim_limit', 0) if config else 0
        
        # Create progress bars
        def create_progress_bar(value, max_value, length=5):
            filled = int((value / max_value) * length) if max_value > 0 else 0
            return '[' + '▰' * filled + '▱' * (length - filled) + ']'
        
        active_bar = create_progress_bar(len(active_claims), limit) if limit > 0 else '[▱▱▱▱▱]'
        closed_bar = create_progress_bar(len(closed_claims), total_claims) if total_claims > 0 else '[▱▱▱▱▱]'
        
        embed = discord.Embed(
            title=f"Claim Statistics for {target.display_name}",
            description=(
                "**CLAIMS USAGE**\n"
                f"Active Claims   {active_bar}  {len(active_claims)}/{limit}\n"
                f"Closed Claims   {closed_bar}  {len(closed_claims)}/{total_claims}\n"
                f"Total Claims: {total_claims}\n\n"
                "**LIMIT STATUS**\n"
                f"Claim Limit: {limit}"
            ),
            color=self.bot.main_color
        )
        
        embed.set_author(name=target.display_name, icon_url=target.avatar.url if target.avatar else None)
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command(name="overview")
    async def claim_overview(self, ctx):
        """Show comprehensive claims overview"""
        stats = {
            'active': 0,
            'closed': 0,
            'total': 0
        }
        
        async for doc in self.ticket_claims_collection.find({
            'guild_id': str(self.bot.modmail_guild.id),
            'claimers': {'$exists': True}
        }):
            stats['total'] += 1
            
            # Check if channel still exists and is not closed
            try:
                channel = self.bot.get_channel(int(doc['thread_id']))
                if channel and ('status' not in doc or doc['status'] != 'closed'):
                    stats['active'] += 1
                else:
                    stats['closed'] += 1
                    # Update status if channel doesn't exist
                    if not channel and ('status' not in doc or doc['status'] != 'closed'):
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )
            except:
                stats['closed'] += 1
                # Update status if there's an error
                if 'status' not in doc or doc['status'] != 'closed':
                    await self.ticket_claims_collection.find_one_and_update(
                        {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                        {'$set': {'status': 'closed'}}
                    )

        if stats['total'] == 0:
            embed = discord.Embed(
                title="Claims Overview",
                description="No claims found",
                color=self.bot.main_color
            )
            return await ctx.send(embed=embed)

        # Calculate closure percentage
        closure_rate = (stats['closed'] / stats['total'] * 100) if stats['total'] > 0 else 0

        embed = discord.Embed(
            title="Claims Overview",
            description=(
                "```\n"
                "╭─── Claims Status ───────────────╮\n"
                f"│  Active     │ {stats['active']:<14} │\n"
                f"│  Closed     │ {stats['closed']:<14} │\n"
                f"│  Total      │ {stats['total']:<14} │\n"
                "├─── Performance ──────────────────┤\n"
                f"│  Closure    │ {closure_rate:.1f}%{' ':<11} │\n"
                "╰──────────────────────────────────╯\n"
                "```"
            ),
            color=self.bot.main_color
        )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.group(name='claimconfig', invoke_without_command=True)
    async def claim_config(self, ctx):
        """Configure claim limit"""
        if ctx.invoked_subcommand is None:
            config = await self.get_config()
            embed = discord.Embed(
                title="Claim Configuration",
                description=f"Current claim limit: **{config['claim_limit']}**\n"
                          f"Use `{ctx.prefix}claimconfig limit <number>` to change",
                color=self.bot.main_color
            )
            await ctx.send(embed=embed)

    @claim_config.command(name='limit')
    async def set_claim_limit(self, ctx, limit: int):
        """Set the maximum number of claims per user (0 for unlimited)"""
        if limit < 0:
            return await ctx.send("Limit cannot be negative")
            
        await self.config_collection.find_one_and_update(
            {'_id': 'claim_config'},
            {'$set': {'claim_limit': limit}},
            upsert=True
        )
        
        limit_text = str(limit) if limit > 0 else "unlimited"
        await ctx.send(f"Claim limit set to {limit_text}")

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.group(name='override', invoke_without_command=True)
    async def claim_override(self, ctx):
        """Manage override roles for claims"""
        if ctx.invoked_subcommand is None:
            config = await self.config_collection.find_one({'_id': 'claim_config'})
            
            override_roles = []
            for role_id in config['override_roles']:
                if role := ctx.guild.get_role(role_id):
                    override_roles.append(role.mention)
            
            embed = discord.Embed(
                title="Claim Override Roles",
                description="Roles that can override claimed tickets:\n" + 
                           ("\n".join(override_roles) if override_roles else "No roles set"),
                color=self.bot.main_color
            )
            await ctx.send(embed=embed)

    @claim_override.command(name='add')
    async def override_add(self, ctx, *, role: discord.Role):
        """Add a role to override claims"""
        config = await self.config_collection.find_one({'_id': 'claim_config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id in override_roles:
            return await ctx.send("That role is already an override role")
            
        override_roles.append(role.id)
        await self.config_collection.find_one_and_update(
            {'_id': 'claim_config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Added {role.mention} to override roles")

    @claim_override.command(name='remove')
    async def override_remove(self, ctx, *, role: discord.Role):
        """Remove a role from override claims"""
        config = await self.config_collection.find_one({'_id': 'claim_config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id not in override_roles:
            return await ctx.send("That role is not an override role")
            
        override_roles.remove(role.id)
        await self.config_collection.find_one_and_update(
            {'_id': 'claim_config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Removed {role.mention} from override roles")

    async def update_ticket_stats(self, thread, closer):
        """
        Update ticket statistics when a thread is closed
        
        Args:
            thread: The thread being closed
            closer: The user who closed the thread
        """
        try:
            # Extensive logging for debugging
            print("🔍 Attempting to update ticket stats:")
            print(f"  Thread: {thread}")
            print(f"  Closer: {closer}")
            
            # Check thread status
            if thread is None:
                print("❌ Thread is None, cannot log stats")
                return
            
            # Determine thread status
            is_closed = getattr(thread, 'closed', False)
            print(f"  Thread Closed Status: {is_closed}")
            
            # Prepare stats document
            stats_doc = {
                'thread_id': str(thread.id),
                'guild_id': str(thread.guild.id) if thread.guild else 'unknown',
                'moderator_id': str(closer.id) if closer else 'unknown',
                'closed_at': datetime.utcnow(),
                'status': 'closed' if is_closed else 'active'
            }
            
            # Log document details
            print("📋 Stats Document:")
            for key, value in stats_doc.items():
                print(f"  {key}: {value}")
            
            # Insert stats document
            try:
                result = await self.ticket_stats_collection.insert_one(stats_doc)
                
                if result.inserted_id:
                    print(f"✅ Ticket stats logged successfully")
                    print(f"  Inserted ID: {result.inserted_id}")
                else:
                    print("❌ Failed to log ticket stats")
            
            except Exception as insert_error:
                print(f"❌ MongoDB Insertion Error: {insert_error}")
                import traceback
                traceback.print_exc()
        
        except Exception as e:
            print(f"❌ Comprehensive Error in update_ticket_stats: {e}")
            import traceback
            traceback.print_exc()

    @commands.command(name="claim")
    @commands.check(is_in_thread)
    async def claim_thread(self, ctx):
        """
        Claim the current ticket thread
        """
        await ctx.send(f"✅ {ctx.author.mention} has acknowledged this ticket.")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    async def unclaim_thread(self, ctx):
        """
        Unclaim the current ticket thread
        """
        await ctx.send(f"✅ Ticket status reset.")

    @commands.command(name="sync_ticket_stats")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def sync_ticket_stats(self, ctx):
        """
        Synchronize ticket stats between different data sources
        
        This command will:
        1. Fetch ticket stats from Modmail's database
        2. Merge with existing stats in the plugin's database
        3. Update the consolidated stats
        """
        try:
            # Fetch ticket stats from Modmail's database
            modmail_stats_collection = self.bot.api.get_shared_partition('ticket_stats')
            modmail_stats = await modmail_stats_collection.find().to_list(length=None)
            
            # Prepare bulk write operations
            bulk_operations = []
            for stat in modmail_stats:
                # Upsert operation to merge or create stats
                bulk_operations.append(
                    UpdateOne(
                        {'user_id': stat['user_id']},
                        {'$set': stat},
                        upsert=True
                    )
                )
            
            # Perform bulk write to consolidate stats
            if bulk_operations:
                result = await self.ticket_stats_collection.bulk_write(bulk_operations)
                await ctx.send(f"Ticket stats synchronized. "
                               f"Matched: {result.matched_count}, "
                               f"Modified: {result.modified_count}, "
                               f"Upserted: {result.upserted_count}")
            else:
                await ctx.send("No ticket stats found to synchronize.")
        
        except Exception as e:
            await ctx.send(f"Error synchronizing ticket stats: {e}")
            print(f"Sync ticket stats error: {e}")

    @commands.command(name="export_ticket_stats")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def export_ticket_stats(self, ctx, days: int = 30):
        """
        Export ticket stats to MongoDB collection
        
        :param days: Number of days to look back (default 30)
        """
        try:
            # Fetch ticket stats
            stats_collection = self.ticket_stats_collection
            
            # Aggregate stats by user
            pipeline = [
                {'$group': {
                    '_id': '$user_id',
                    'total_tickets': {'$sum': '$closed_tickets'},
                    'last_activity': {'$max': '$timestamp'}
                }},
                {'$sort': {'total_tickets': -1}}
            ]
            
            stats = await stats_collection.aggregate(pipeline).to_list(length=None)
            
            # Create formatted output
            stats_message = "Ticket Stats Export:\n"
            for stat in stats:
                stats_message += (
                    f"User ID: {stat['_id']}, "
                    f"Total Tickets Closed: {stat['total_tickets']}, "
                    f"Last Activity: {stat['last_activity']}\n"
                )
            
            # Send stats to channel
            await ctx.send(f"```\n{stats_message}\n```")
        
        except Exception as e:
            await ctx.send(f"Error exporting ticket stats: {e}")
            print(f"Export ticket stats error: {e}")

    async def track_ticket_count(self, user_id: int, timestamp: datetime = None):
        """
        Track ticket count for a specific user by day and month
        
        :param user_id: Discord user ID
        :param timestamp: Timestamp of the ticket (defaults to current time)
        :return: Dictionary of ticket statistics
        """
        if timestamp is None:
            timestamp = datetime.utcnow()
        
        # Prepare document for daily ticket count
        daily_doc = {
            'user_id': user_id,
            'date': timestamp.date(),
            'month': timestamp.strftime('%Y-%m'),
            'ticket_count': 1
        }
        
        try:
            # Upsert daily ticket count
            await self.ticket_stats_collection.update_one(
                {
                    'user_id': user_id, 
                    'date': daily_doc['date']
                },
                {'$inc': {'ticket_count': 1}},
                upsert=True
            )
            
            # Aggregate monthly ticket count
            monthly_result = await self.ticket_stats_collection.aggregate([
                {
                    '$match': {
                        'user_id': user_id,
                        'month': daily_doc['month']
                    }
                },
                {
                    '$group': {
                        '_id': '$month',
                        'total_tickets': {'$sum': '$ticket_count'}
                    }
                }
            ]).to_list(length=1)
            
            # Optional: You can store monthly aggregates in a separate collection if needed
            if monthly_result:
                await self.ticket_stats_collection.update_one(
                    {
                        'user_id': user_id,
                        'month': daily_doc['month'],
                        'is_monthly_aggregate': True
                    },
                    {'$set': monthly_result[0]},
                    upsert=True
                )
        
        except Exception as e:
            print(f"Error tracking ticket count for user {user_id}: {e}")
            return {}

    @commands.command(name="set_export_webhook")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def set_export_webhook(self, ctx, webhook_url: str):
        """
        Set the webhook URL for ticket exports
        
        Usage: !set_export_webhook https://discord.com/api/webhooks/...
        """
        try:
            # Validate webhook URL
            if not webhook_url.startswith('https://discord.com/api/webhooks/'):
                await ctx.send("Invalid Discord webhook URL. Please provide a valid Discord webhook.")
                return
            
            # Save to MongoDB
            config_collection = self.config_collection
            await config_collection.update_one(
                {'_id': 'ticket_export_config'},
                {'$set': {'webhook_url': webhook_url}},
                upsert=True
            )
            
            # Update instance variable
            self.ticket_export_webhook = webhook_url
            
            await ctx.send("Ticket export webhook URL has been set successfully!")
        except Exception as e:
            await ctx.send(f"Error setting webhook: {e}")
            print(f"Webhook set error: {e}")

    @commands.command(name="exportclaims")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def export_claims_command(self, ctx):
        """
        Manual command to export claimed tickets
        """
        if not self.ticket_export_webhook:
            await ctx.send("Webhook URL is not set. Use !set_export_webhook first.")
            return
        
        await ctx.send("Exporting claimed tickets...")
        filename = await self.export_claimed_tickets()
        
        if filename:
            await ctx.send(f"Exported claimed tickets to {filename}")
        else:
            await ctx.send("Failed to export tickets. Check the logs for more information.")

    async def export_claimed_tickets(self, days=None):
        """
        Export claimed tickets to a file
        
        :param days: Number of days to look back
        :return: Filename or CSV content
        """
        # Prepare query
        query = {
            'guild_id': str(self.bot.modmail_guild.id),
            'claimers': {'$exists': True}
        }
        
        # Add date filter if days specified
        if days:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            query['claimed_at'] = {'$gte': cutoff_date}
        
        ticket_data = []
        
        # Fetch tickets
        async for doc in self.ticket_claims_collection.find(query):
            ticket_data.append({
                'user_id': doc.get('user_id', ''),
                'thread_id': doc.get('thread_id', ''),
                'status': doc.get('status', ''),
                'claimed_at': doc.get('claimed_at', '')
            })
        
        # Create DataFrame or CSV
        if self.pandas_available:
            df = pd.DataFrame(ticket_data)
            filename = f"claimed_tickets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            df.to_excel(filename, index=False)
            return filename
        else:
            # Fallback CSV export without pandas
            csv_content = "User ID,Thread ID,Status,Claimed At\n"
            for ticket in ticket_data:
                csv_content += f"{ticket.get('user_id', '')},{ticket.get('thread_id', '')},{ticket.get('status', '')},{ticket.get('claimed_at', '')}\n"
            
            filename = f"claimed_tickets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(filename, 'w') as f:
                f.write(csv_content)
            
            return filename

    @commands.command(name="thread_claim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_claim(self, ctx):
        """
        Claim the current thread
        Prevents other supporters from replying
        """
        try:
            # Check if thread is already claimed
            existing_claim = await self.ticket_claims_collection.find_one({
                'thread_id': str(ctx.thread.channel.id),
                'guild_id': str(ctx.guild.id)
            })

            if existing_claim:
                # Check if current user is already a claimer
                if str(ctx.author.id) in existing_claim.get('claimers', []):
                    await ctx.send("You have already claimed this thread.")
                    return
                
                # Check claim limits
                config = await self.get_config()
                user_claims = await self.count_active_claims(ctx.author.id)
                
                if user_claims >= config.get('claim_limit', 5):
                    await ctx.send(f"You have reached the maximum claim limit of {config['claim_limit']} threads.")
                    return
                
                # Add current user to claimers
                await self.ticket_claims_collection.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild_id': str(ctx.guild.id)},
                    {'$addToSet': {'claimers': str(ctx.author.id)}}
                )
            else:
                # Create new claim
                await self.ticket_claims_collection.insert_one({
                    'thread_id': str(ctx.thread.channel.id),
                    'guild_id': str(ctx.guild.id),
                    'claimers': [str(ctx.author.id)],
                    'claimed_at': datetime.utcnow(),
                    'status': 'active'
                })
            
            await ctx.send(f"Thread claimed by {ctx.author.mention}")
        
        except Exception as e:
            await ctx.send(f"Error claiming thread: {e}")
            print(f"Claim thread error: {e}")

    async def count_active_claims(self, user_id):
        """
        Count active claims for a specific user
        
        :param user_id: ID of the user
        :return: Number of active claims
        """
        active_claims = await self.ticket_claims_collection.count_documents({
            'claimers': str(user_id),
            'status': 'active'
        })
        return active_claims

    @commands.command(name="thread_unclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_unclaim(self, ctx):
        """
        Unclaim the current thread
        Allows other supporters to claim and reply
        """
        try:
            result = await self.ticket_claims_collection.find_one_and_update(
                {
                    'thread_id': str(ctx.thread.channel.id),
                    'guild_id': str(ctx.guild.id),
                    'claimers': str(ctx.author.id)
                },
                {
                    '$pull': {'claimers': str(ctx.author.id)},
                    '$set': {'status': 'unclaimed' if len(self.claimers) == 0 else 'active'}
                }
            )
            
            if result:
                await ctx.send(f"Thread unclaimed by {ctx.author.mention}")
            else:
                await ctx.send("You haven't claimed this thread.")
        
        except Exception as e:
            await ctx.send(f"Error unclaiming thread: {e}")
            print(f"Unclaim thread error: {e}")

    @commands.command(name="set_claim_limit")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def set_claim_limit(self, ctx, limit: int):
        """
        Set the maximum number of threads a supporter can claim
        
        :param limit: Maximum number of active claims
        """
        if limit < 0:
            await ctx.send("Claim limit cannot be negative.")
            return
        
        await self.config_collection.update_one(
            {'_id': 'claim_config'},
            {'$set': {'claim_limit': limit}},
            upsert=True
        )
        
        await ctx.send(f"Claim limit set to {limit} threads.")

    @commands.command(name="thread_claims")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_claims(self, ctx):
        """Check which channels you have claimed"""
        cursor = self.ticket_claims_collection.find({'guild_id':str(self.bot.modmail_guild.id)})
        active_channels = []
        
        async for doc in cursor:
            try:
                if str(ctx.author.id) in doc.get('claimers', []):
                    channel = self.bot.get_channel(int(doc['thread_id']))
                    if channel:
                        active_channels.append(channel)
                    else:
                        await self.ticket_claims_collection.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                            {'$set': {'status': 'closed'}}
                        )
            except discord.NotFound:
                await self.ticket_claims_collection.find_one_and_update(
                    {'thread_id': doc['thread_id'], 'guild_id': doc['guild_id']},
                    {'$set': {'status': 'closed'}}
                )
        
        if active_channels:
            channel_list = "\n".join(channel.mention for channel in active_channels)
            await ctx.send(f"Your claimed threads:\n{channel_list}")
        else:
            await ctx.send("You have no active claimed threads.")

    async def get_ticket_stats_for_placeholder(self, user_id: int, stat_type: str = 'daily', time_period: int = 30):
        """
        Retrieve ticket statistics for placeholders

        :param user_id: Discord user ID
        :param stat_type: Type of statistic ('daily', 'monthly')
        :param time_period: Number of days to look back
        :return: Dictionary of ticket statistics
        """
        try:
            now = datetime.utcnow()
            time_threshold = now - timedelta(days=time_period)

            if stat_type == 'daily':
                # Daily ticket count for the specified time period
                daily_stats = await self.ticket_stats_collection.find({
                    'user_id': user_id,
                    'date': {'$gte': time_threshold.date()},
                    'is_monthly_aggregate': {'$ne': True}
                }).to_list(length=None)

                return {
                    'total_daily_tickets': sum(stat.get('ticket_count', 0) for stat in daily_stats),
                    'daily_ticket_details': daily_stats
                }

            elif stat_type == 'monthly':
                # Monthly ticket count
                monthly_stats = await self.ticket_stats_collection.find({
                    'user_id': user_id,
                    'is_monthly_aggregate': True,
                    '_id': {'$gte': (now - timedelta(days=time_period)).strftime('%Y-%m')}
                }).to_list(length=None)

                return {
                    'total_monthly_tickets': sum(stat.get('total_tickets', 0) for stat in monthly_stats),
                    'monthly_ticket_details': monthly_stats
                }

        except Exception as e:
            print(f"Error retrieving ticket stats for user {user_id}: {e}")
            return {}

    @commands.command(name="ticket_stats_details")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def ticket_stats_details(self, ctx, member: discord.Member = None):
        """
        Show detailed ticket statistics for a user or yourself

        :param ctx: Command context
        :param member: Optional member to check stats for (defaults to command invoker)
        """
        member = member or ctx.author
        
        try:
            # Get daily and monthly stats
            daily_stats = await self.get_ticket_stats_for_placeholder(member.id, 'daily')
            monthly_stats = await self.get_ticket_stats_for_placeholder(member.id, 'monthly')

            # Create an embed to display stats
            embed = discord.Embed(
                title=f"📊 Ticket Statistics for {member.display_name}",
                color=discord.Color.blue()
            )

            # Add daily ticket stats
            embed.add_field(
                name="Daily Ticket Count (Last 30 Days)",
                value=f"Total Tickets: {daily_stats.get('total_daily_tickets', 0)}",
                inline=False
            )

            # Add monthly ticket stats
            embed.add_field(
                name="Monthly Ticket Count",
                value=f"Total Tickets: {monthly_stats.get('total_monthly_tickets', 0)}",
                inline=False
            )

            await ctx.send(embed=embed)

        except Exception as e:
            await ctx.send(f"Error retrieving ticket statistics: {e}")

    @commands.command(name='mongodb_debug')
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def mongodb_debug(self, ctx):
        """
        Debug MongoDB connection and ticket statistics
        """
        try:
            # Test MongoDB connection
            await self.mongo_db.command('ping')

            # Get ticket stats collection info
            collection_stats = await self.ticket_stats_collection.find_one()
            total_documents = await self.ticket_stats_collection.count_documents({})

            # Create debug embed
            embed = discord.Embed(
                title="🔍 MongoDB Debug Information",
                color=discord.Color.green()
            )
            embed.add_field(name="Connection Status", value="✅ Successfully Connected", inline=False)
            embed.add_field(name="Database", value=self.mongo_db.name, inline=False)
            embed.add_field(name="Total Ticket Stat Documents", value=str(total_documents), inline=False)
            
            # Sample document if exists
            if collection_stats:
                sample_doc = "\n".join([f"{k}: {v}" for k, v in collection_stats.items()])
                embed.add_field(name="Sample Document", value=f"```\n{sample_doc}\n```", inline=False)

            await ctx.send(embed=embed)

        except Exception as e:
            # Create error embed
            embed = discord.Embed(
                title="❌ MongoDB Connection Error",
                description=str(e),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            # Re-raise the exception to ensure it's logged
            raise

    async def debug_mongodb_connection(self):
        """
        Debug MongoDB connection and collections
        """
        try:
            print("🔍 MongoDB Connection Debug:")
            
            # Check MongoDB client
            if not hasattr(self, 'mongo_client'):
                print("❌ MongoDB client not initialized")
                return
            
            # Check database
            if not hasattr(self, 'mongo_db'):
                print("❌ MongoDB database not initialized")
                return
            
            # List collections
            try:
                collection_names = await self.mongo_db.list_collection_names()
                print("📋 Existing Collections:")
                for name in collection_names:
                    print(f"  - {name}")
            except Exception as list_error:
                print(f"❌ Error listing collections: {list_error}")
            
            # Check ticket_stats collection
            try:
                stats_count = await self.ticket_stats_collection.count_documents({})
                print(f"📊 Ticket Stats Collection:")
                print(f"  Total Documents: {stats_count}")
                
                # Fetch recent documents for debugging
                recent_stats = await self.ticket_stats_collection.find().sort('closed_at', -1).limit(5).to_list(length=5)
                print("  Recent Entries:")
                for stat in recent_stats:
                    print(f"    - {stat}")
            
            except Exception as stats_error:
                print(f"❌ Error checking ticket stats: {stats_error}")
        
        except Exception as e:
            print(f"❌ Comprehensive MongoDB Debug Error: {e}")
            import traceback
            traceback.print_exc()

# Removed check_reply function to resolve MongoDB collection errors
# This function was causing issues with collection method calls

async def setup(bot):
    """
    Asynchronous setup function for the plugin
    
    :param bot: Discord bot instance
    """
    try:
        # Ensure the cog is added asynchronously
        await bot.add_cog(ClaimThread(bot))
    except Exception as e:
        print(f"Error setting up ClaimThread plugin: {e}")
        raise
