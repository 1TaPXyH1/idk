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
from types import SimpleNamespace

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
            # Establish MongoDB connection
            self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(self.mongo_uri)
            
            # Select database
            self.mongo_db = self.mongo_client[self.mongo_db_name]
            
            # Initialize collections with error suppression
            try:
                # Ticket stats collection
                self.ticket_stats_collection = self.mongo_db['ticket_stats']
                
                # Configuration collection
                self.config_collection = self.mongo_db['plugin_configs']
                
                print("✅ Verified Collections")
            except Exception as collection_error:
                print(f"❌ Collection Initialization Error: {collection_error}")
                # Ensure collections exist even if initialization fails
                self.ticket_stats_collection = self.mongo_db['ticket_stats']
                self.config_collection = self.mongo_db['plugin_configs']
            
            # Verify plugin configuration collection
            config_exists = await self.config_collection.find_one({'_id': 'claim_config'})
            if not config_exists:
                await self.config_collection.insert_one({
                    '_id': 'claim_config',
                    'claim_limit': 5,
                    'override_roles': []
                })
                print("ℹ️ Created initial plugin configuration")
            else:
                print("ℹ️ Plugin Configurations Collection already exists: plugin_configs")
        
        except Exception as e:
            print(f"❌ MongoDB Initialization Error: {e}")
            import traceback
            traceback.print_exc()

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

        # Start background ticket state verification
        self.bot.loop.create_task(self.background_ticket_state_check())

        # Start background thread existence verification
        self.bot.loop.create_task(self.background_thread_existence_check())

    async def background_thread_existence_check(self):
        """
        Periodic background task to verify thread existence and update status
        Runs every 5 seconds to check active tickets
        """
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                # Find all non-closed tickets
                active_tickets = await self.ticket_stats_collection.find({
                    'current_state': {'$ne': 'closed'}
                }).to_list(length=None)
                
                for ticket in active_tickets:
                    try:
                        thread_id = int(ticket['thread_id'])
                        guild_id = int(ticket.get('guild_id', 0))
                        
                        # Find the guild
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            # If guild not found, skip this ticket
                            continue
                        
                        # Try to fetch the thread channel
                        try:
                            thread = guild.get_thread(thread_id)
                            
                            # Perform additional checks
                            if thread is None:
                                # Attempt to fetch the channel to confirm complete deletion
                                try:
                                    await guild.fetch_channel(thread_id)
                                    # Channel still exists but not a thread
                                    continue
                                except discord.NotFound:
                                    # Channel completely deleted
                                    await self.on_thread_state_change(
                                        SimpleNamespace(id=thread_id, guild=guild), 
                                        'closed'
                                    )
                                except Exception as fetch_error:
                                    print(f"⚠️ Error fetching channel {thread_id}: {fetch_error}")
                                    continue
                            
                            # Check if thread is actually closed
                            if thread.closed:
                                await self.on_thread_state_change(
                                    thread, 
                                    'closed'
                                )
                        
                        except Exception as thread_error:
                            print(f"⚠️ Error checking thread {thread_id}: {thread_error}")
                    
                    except Exception as ticket_error:
                        print(f"⚠️ Error processing ticket {ticket.get('thread_id', 'unknown')}: {ticket_error}")
            
            except Exception as e:
                print(f"❌ Background thread existence check failed: {e}")
            
            # Wait for 5 seconds before next check
            await asyncio.sleep(5)

    async def background_ticket_state_check(self):
        """
        Periodic background task to verify and update ticket states
        """
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                # Find all tickets not marked as closed
                open_tickets = await self.ticket_stats_collection.find({
                    '$or': [
                        {'current_state': {'$ne': 'closed'}},
                        {'is_closed': False}
                    ]
                }).to_list(length=None)
                
                for ticket in open_tickets:
                    try:
                        # Attempt to fetch the thread
                        thread_id = int(ticket['thread_id'])
                        guild_id = int(ticket.get('guild_id', 0))
                        
                        # Find the guild and thread
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            # If guild not found, mark as closed
                            await self.on_thread_state_change(
                                SimpleNamespace(id=thread_id, guild=None), 
                                'closed'
                            )
                            continue
                        
                        thread = guild.get_thread(thread_id)
                        
                        # Check thread state
                        if thread is None or thread.closed:
                            # Dispatch closed state if thread is not found or closed
                            await self.on_thread_state_change(
                                thread or SimpleNamespace(id=thread_id, guild=guild), 
                                'closed'
                            )
                    
                    except Exception as ticket_error:
                        print(f"⚠️ Error checking ticket {ticket.get('thread_id', 'unknown')}: {ticket_error}")
            
            except Exception as e:
                print(f"❌ Background ticket state check failed: {e}")
            
            # Wait before next check (15 minutes)
            await asyncio.sleep(900)  # 15 minutes instead of 1 hour

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
        
        async for doc in self.ticket_stats_collection.find(
            {
                'guild_id': str(self.bot.modmail_guild.id),
                'last_user_id': {'$exists': True}
            }
        ):
            # Skip if before cutoff date
            if cutoff_date and 'created_at' in doc:
                created_at = datetime.fromisoformat(doc['created_at'])
                if created_at < cutoff_date:
                    continue
                    
            last_user_id = doc['last_user_id']
            total_claims[last_user_id] = total_claims.get(last_user_id, 0) + 1
            # Check if claim is active
            if doc['current_state'] != 'closed':
                claims[last_user_id] = claims.get(last_user_id, 0) + 1

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
        try:
            # Only handle subscription
            if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                self.bot.config["subscriptions"][str(ctx.thread.id)] = []
            if ctx.author.mention not in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                self.bot.config["subscriptions"][str(ctx.thread.id)].append(ctx.author.mention)
                await self.bot.config.update()

            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"Successfully claimed the thread.\n{ctx.author.mention} is now subscribed to this thread."
            )
            await ctx.send(embed=embed)
        except:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def thread_unclaim(self, ctx):
        """Unclaim a thread without renaming"""
        try:
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
            
        await self.config_collection.update_one(
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
        await self.config_collection.update_one(
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
        await self.config_collection.update_one(
            {'_id': 'claim_config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Removed {role.mention} from override roles")

    async def update_ticket_stats(self, thread, closer):
        """
        Update ticket statistics when a thread is created, claimed, unclaimed, or closed
        
        Args:
            thread: The thread being tracked
            closer: The user who performed the action (can be None)
        """
        try:
            # Extensive logging for debugging
            print("🔍 Attempting to update ticket stats:")
            print(f"  Thread: {thread}")
            print(f"  Closer: {closer}")
            
            # Check thread status and existence
            if thread is None:
                print("❌ Thread is None, cannot log stats")
                return
            
            # Determine thread status and lifecycle
            is_closed = False
            try:
                # Check if thread exists and is closed
                is_closed = thread.closed if hasattr(thread, 'closed') else False
                
                # Additional check for thread existence
                if hasattr(thread, 'guild'):
                    try:
                        # Attempt to fetch the thread to verify its existence
                        await thread.guild.fetch_channel(thread.id)
                    except discord.NotFound:
                        # Thread no longer exists, mark as closed
                        is_closed = True
                    except Exception as fetch_error:
                        print(f"⚠️ Error fetching thread: {fetch_error}")
            except Exception as status_error:
                print(f"⚠️ Error checking thread status: {status_error}")
                is_closed = False
            
            thread_id = str(thread.id)
            guild_id = str(thread.guild.id) if thread.guild else 'unknown'
            
            # Prepare stats document
            stats_doc = {
                'thread_id': thread_id,
                'guild_id': guild_id,
                'created_at': thread.created_at,
                'status': 'closed' if is_closed else 'open',
                'lifecycle': {
                    'created': True,
                    'closed': is_closed
                }
            }
            
            # Handle moderator information
            if closer:
                stats_doc['moderator_id'] = str(closer.id)
            else:
                # If no closer provided, use null/None
                stats_doc['moderator_id'] = None
            
            # Add closure timestamp if closed
            if is_closed:
                stats_doc['closed_at'] = datetime.utcnow()
            else:
                stats_doc['closed_at'] = None
            
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
        
        # Dispatch state change event
        await self.on_thread_state_change(thread, 'claimed', ctx.author)

        await ctx.send(f"✅ {ctx.author.mention} has acknowledged this ticket.")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    async def unclaim_thread(self, ctx):
        """
        Unclaim the current ticket thread
        """
        # Get the current thread
        thread = ctx.channel

        # Check if thread is closed
        is_thread_closed = thread.closed if hasattr(thread, 'closed') else False

        # Log ticket stats when unclaimed
        await self.update_ticket_stats(thread, None if is_thread_closed else ctx.author)
        
        # Dispatch state change event
        await self.on_thread_state_change(thread, 'unclaimed', ctx.author)

        await ctx.send(f"✅ Ticket status reset.")

    @commands.command(name="thread_claim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_claim(self, ctx):
        """
        Claim the current thread
        Prevents other supporters from replying
        """
        try:
            # Check claim limits
            config = await self.get_config()
            user_claims = await self.count_active_claims(ctx.author.id)
            
            if user_claims >= config.get('claim_limit', 5):
                await ctx.send(f"You have reached the maximum claim limit of {config['claim_limit']} threads.")
                return
            
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
        try:
            # Count active claims using ticket stats collection
            active_claims = await self.ticket_stats_collection.count_documents({
                'last_user_id': str(user_id),
                'current_state': {'$ne': 'closed'}
            })
            return active_claims
        except Exception as e:
            print(f"❌ Error counting active claims: {e}")
            return 0

    @commands.command(name="thread_unclaim")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_unclaim(self, ctx):
        """
        Unclaim the current thread
        Allows other supporters to claim and reply
        """
        try:
            await ctx.send(f"Thread unclaimed by {ctx.author.mention}")
        
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

    async def on_thread_state_change(self, thread, state, user=None):
        """
        Centralized event listener for ticket state changes
        
        Args:
            thread: The Discord thread
            state: New state of the ticket ('claimed', 'unclaimed', 'closed')
            user: User who triggered the state change (optional)
        """
        try:
            # Determine if thread exists and is closed
            is_closed = state == 'closed'
            
            # Check thread existence and status
            try:
                if thread and hasattr(thread, 'guild'):
                    # Attempt to fetch the channel to verify existence
                    channel = await thread.guild.fetch_channel(thread.id)
                    
                    # If it's a thread, check its actual status
                    if isinstance(channel, discord.Thread):
                        is_closed = channel.closed
                    else:
                        # If it's not a thread, consider it closed
                        is_closed = True
                else:
                    is_closed = True
            except discord.NotFound:
                # Channel completely deleted
                is_closed = True
            except Exception as fetch_error:
                print(f"⚠️ Error checking thread existence: {fetch_error}")
                is_closed = False
            
            # Prepare minimal stats document
            stats_doc = {
                'thread_id': str(thread.id) if thread else 'unknown',
                'guild_id': str(thread.guild.id) if thread and thread.guild else 'unknown',
                'current_state': state,
                'status': 'closed' if is_closed else 'open',
                'is_closed': is_closed,
                'last_updated': datetime.utcnow(),
                'closed_at': datetime.utcnow() if is_closed else None,
                'last_user_id': str(user.id) if user else None,
                'moderator_id': str(user.id) if user else None
            }
            
            # Upsert ticket stats
            result = await self.ticket_stats_collection.update_one(
                {'thread_id': stats_doc['thread_id']},
                {'$set': stats_doc},
                upsert=True
            )
            
            # Minimal logging
            print(f"📋 Ticket State: {stats_doc['thread_id']} -> {state} (Closed: {is_closed})")
            
        except Exception as e:
            print(f"❌ Error tracking ticket state: {e}")
            import traceback
            traceback.print_exc()

    async def verify_thread_closure(self, thread_id, timeout=300):
        """
        Verify if a thread is actually closed
        
        :param thread_id: ID of the thread to check
        :param timeout: Maximum time to wait for closure (in seconds)
        :return: Boolean indicating if thread is closed
        """
        start_time = datetime.utcnow()
        
        while (datetime.utcnow() - start_time).total_seconds() < timeout:
            try:
                # Attempt to fetch the thread
                thread = self.bot.get_channel(thread_id)
                
                # Check thread state
                if thread is None:
                    # Thread completely deleted
                    return True
                
                if thread.closed:
                    # Thread is archived/closed
                    return True
                
                # Check if thread has no recent messages
                try:
                    recent_messages = await thread.history(limit=1).flatten()
                    if not recent_messages:
                        return True
                except Exception:
                    # If history fetch fails, it might indicate closure
                    return True
                
                # Wait before next check
                await asyncio.sleep(10)  # Check every 10 seconds
            
            except Exception as e:
                print(f"❌ Closure verification error: {e}")
                return False
        
        # Timeout reached without confirmation
        return False

async def setup(bot):
    """
    Asynchronous setup function for the plugin
    
    :param bot: Discord bot instance
    """
    await bot.add_cog(ClaimThread(bot))
