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
        Update ticket statistics when a thread is created or closed
        
        Args:
            thread: The thread being tracked
            closer: The user who closed the thread (optional)
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
            
            # Determine thread status and lifecycle
            is_closed = getattr(thread, 'closed', False)
            thread_id = str(thread.id)
            guild_id = str(thread.guild.id) if thread.guild else 'unknown'
            
            # Prepare stats document
            stats_doc = {
                'thread_id': thread_id,
                'guild_id': guild_id,
                'moderator_id': str(closer.id) if closer else 'unknown',
                'created_at': thread.created_at,
                'closed_at': datetime.utcnow() if is_closed else None,
                'status': 'closed' if is_closed else 'open',
                'lifecycle': {
                    'created': True,
                    'closed': is_closed
                }
            }
            
            # Log document details
            print("📋 Stats Document:")
            for key, value in stats_doc.items():
                print(f"  {key}: {value}")
            
            # Insert or update stats document
            try:
                # Try to find existing document for this thread
                existing_doc = await self.ticket_stats_collection.find_one({
                    'thread_id': thread_id,
                    'guild_id': guild_id
                })
                
                if existing_doc:
                    # Update existing document
                    update_result = await self.ticket_stats_collection.update_one(
                        {'_id': existing_doc['_id']},
                        {'$set': stats_doc}
                    )
                    print(f"✅ Updated existing ticket stats: {update_result.modified_count} document(s)")
                else:
                    # Insert new document
                    result = await self.ticket_stats_collection.insert_one(stats_doc)
                    print(f"✅ Inserted new ticket stats. Inserted ID: {result.inserted_id}")
            
            except Exception as insert_error:
                print(f"❌ MongoDB Insertion/Update Error: {insert_error}")
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
        # Get the current thread
        thread = ctx.channel

        # Log ticket stats when claimed
        await self.update_ticket_stats(thread, ctx.author)

        await ctx.send(f"✅ {ctx.author.mention} has acknowledged this ticket.")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    async def unclaim_thread(self, ctx):
        """
        Unclaim the current ticket thread
        """
        # Get the current thread
        thread = ctx.channel

        # Log ticket stats when unclaimed
        await self.update_ticket_stats(thread, ctx.author)

        await ctx.send(f"✅ Ticket status reset.")

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

    # Removed thread_claims method

    async def check_reply(self, ctx):
        """
        Comprehensive debugging method for reply checks
        
        Diagnoses issues with thread claims and reply permissions
        """
        try:
            print("🔍 Debugging check_reply:")
            print(f"  Context: {ctx}")
            print(f"  Command: {ctx.command}")
            print(f"  Channel: {ctx.channel}")
            print(f"  Author: {ctx.author}")
            
            # Check if thread attribute exists
            if not hasattr(ctx, 'thread'):
                print("❌ No thread attribute found")
                return True
            
            # Get the cog
            cog = ctx.bot.get_cog('ClaimThread')
            if not cog:
                print("❌ ClaimThread cog not found")
                return True
            
            # Debug cog attributes
            print("🔍 Cog Attributes:")
            cog_attrs = [
                'ticket_claims_collection', 
                'ticket_stats_collection', 
                'mongo_client', 
                'mongo_db', 
                'check_message_cache'
            ]
            for attr in cog_attrs:
                if hasattr(cog, attr):
                    print(f"  ✅ {attr} exists")
                else:
                    print(f"  ❌ {attr} does not exist")
            
            # Check for collections and database
            try:
                # Attempt to access collections
                if hasattr(cog, 'ticket_claims_collection'):
                    claims_count = await cog.ticket_claims_collection.count_documents({})
                    print(f"📊 Ticket Claims Collection:")
                    print(f"  Total Documents: {claims_count}")
            
                if hasattr(cog, 'ticket_stats_collection'):
                    stats_count = await cog.ticket_stats_collection.count_documents({})
                    print(f"📊 Ticket Stats Collection:")
                    print(f"  Total Documents: {stats_count}")
        
            except Exception as collection_error:
                print(f"❌ Error accessing collections: {collection_error}")
            
            # Check message cache
            if hasattr(cog, 'check_message_cache'):
                print("📋 Message Cache:")
                for channel_id, timestamp in cog.check_message_cache.items():
                    print(f"  Channel {channel_id}: {timestamp}")
            
            # Attempt to find claim
            try:
                # Use getattr with a fallback to prevent AttributeError
                claims_collection = getattr(cog, 'ticket_claims_collection', None)
                
                if claims_collection:
                    claim = await claims_collection.find_one({
                        'thread_id': str(ctx.channel.id)
                    })
                    print(f"🔍 Claim found: {claim}")
                else:
                    print("❌ No claims collection available")
        
            except Exception as claim_error:
                print(f"❌ Error finding claim: {claim_error}")
                import traceback
                traceback.print_exc()
        
            return True
        
        except Exception as e:
            print(f"❌ Comprehensive Error in check_reply: {e}")
            import traceback
            traceback.print_exc()
            return True

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
