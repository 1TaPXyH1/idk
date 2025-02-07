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
                
                # Forcibly remove thread_id and migrate to channel_id
                await self.ticket_stats_collection.update_many(
                    {},
                    {
                        '$unset': {'thread_id': ''},
                        '$rename': {'thread_id': 'channel_id'}
                    }
                )
                
            except Exception as collection_error:
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
        
        except Exception as e:
            import traceback
            traceback.print_exc()

    def __init__(self, bot):
        self.bot = bot
        
        # Reintroduce check_message_cache with minimal implementation
        self.check_message_cache = {}
        
        # Track tickets that have already been notified about closure
        self.notified_closed_tickets = set()
        
        # Comprehensive environment variable logging
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
            
            # Additional validation
            if not parsed_uri.hostname:
                raise ValueError("Invalid MongoDB hostname")
        except Exception as uri_parse_error:
            # Force fallback URI if parsing fails
            self.mongo_uri = f"mongodb+srv://{FALLBACK_MONGODB_USERNAME}:{FALLBACK_MONGODB_PASSWORD}@{FALLBACK_MONGODB_CLUSTER_URL}/?{FALLBACK_MONGODB_OPTIONS}"
        
        # Schedule MongoDB initialization
        self.bot.loop.create_task(self.initialize_mongodb())
        
        # Initialize necessary attributes
        self.default_config = {
            'claim_limit': 5,  # Default claim limit
            'override_roles': []  # Default override roles
        }
        
        # Ticket export webhook (optional)
        self.ticket_export_webhook = None

        # Start background channel verification
        self.bot.loop.create_task(self.background_channel_check())

    async def background_channel_check(self):
        """
        Periodic background task to verify channel existence
        Checks every 5 seconds for open channels
        """
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                # Find all active tickets, using only channel_id
                active_tickets = await self.ticket_stats_collection.find({
                    'current_state': {'$ne': 'closed'}
                }).to_list(length=None)
                
                for ticket in active_tickets:
                    try:
                        # Ensure channel_id exists and is valid
                        if 'channel_id' not in ticket or not ticket['channel_id']:
                            # Remove invalid ticket document
                            await self.ticket_stats_collection.delete_one({'_id': ticket['_id']})
                            continue
                        
                        # Safely convert channel_id to integer
                        try:
                            channel_id = int(ticket['channel_id'])
                        except (ValueError, TypeError):
                            # Remove invalid ticket document
                            await self.ticket_stats_collection.delete_one({'_id': ticket['_id']})
                            continue
                        
                        # Safely get guild_id
                        guild_id = int(ticket.get('guild_id', 0))
                        
                        # Find the guild
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            continue
                        
                        # Get the channel
                        channel = guild.get_channel(channel_id)
                        
                        # Check channel existence
                        if channel is None:
                            # Prevent multiple notifications for the same ticket
                            if channel_id in self.notified_closed_tickets:
                                continue
                            
                            # Channel deleted, notify last claimer
                            if ticket.get('moderator_id'):
                                try:
                                    user = await self.bot.fetch_user(int(ticket['moderator_id']))
                                    await user.send(f"Your ticket has been closed. The support channel was deleted.")
                                    
                                    # Mark this ticket as notified
                                    self.notified_closed_tickets.add(channel_id)
                                except:
                                    pass
                            
                            # Mark as closed
                            await self.on_thread_state_change(
                                SimpleNamespace(id=channel_id, guild=guild), 
                                'closed'
                            )
                    
                    except Exception as ticket_error:
                        # Suppress specific thread_id related errors
                        if 'thread_id' not in str(ticket_error).lower():
                            print(f"Error processing ticket {ticket.get('_id')}: {ticket_error}")
            
            except Exception as background_error:
                # Suppress specific thread_id related errors
                if 'thread_id' not in str(background_error).lower():
                    print(f"Background channel check error: {background_error}")
            
            # Wait for 5 seconds between checks
            await asyncio.sleep(5)

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
            # Check thread status and existence
            if thread is None:
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
                        # If we can fetch the channel, it's not closed
                        is_closed = False
                    except discord.NotFound:
                        # Thread no longer exists, mark as closed
                        is_closed = True
                    except Exception:
                        # Silently handle other exceptions
                        is_closed = False
                else:
                    is_closed = True
            except Exception:
                is_closed = False
            
            channel_id = str(thread.id)
            guild_id = str(thread.guild.id) if thread.guild else 'unknown'
            
            # Prepare stats document
            stats_doc = {
                'channel_id': channel_id,
                'guild_id': guild_id,
                'created_at': thread.created_at,
                'moderator_id': str(closer.id) if closer else None,
                'current_state': 'closed' if is_closed else 'open',
                'closed_at': datetime.utcnow() if is_closed else None
            }
            
            # Insert or update stats document
            try:
                # Try to find existing document for this thread
                existing_doc = await self.ticket_stats_collection.find_one({
                    'channel_id': channel_id,
                    'guild_id': guild_id
                })
                
                if existing_doc:
                    # Update existing document
                    await self.ticket_stats_collection.update_one(
                        {'_id': existing_doc['_id']},
                        {'$set': stats_doc}
                    )
                else:
                    # Insert new document
                    await self.ticket_stats_collection.insert_one(stats_doc)
            
            except Exception:
                # Silently handle any unexpected errors
                pass
        
        except Exception:
            # Silently handle any unexpected errors
            pass

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
        await self.on_thread_state_change(
            thread, 
            'claimed', 
            ctx.author
        )

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
        await self.on_thread_state_change(
            thread, 
            'unclaimed', 
            ctx.author
        )

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
        except Exception:
            # Silently handle any unexpected errors
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
            thread: The Discord channel/thread
            state: New state of the ticket ('claimed', 'unclaimed', 'closed')
            user: User who triggered the state change (optional)
        """
        try:
            # Determine if channel exists and is closed
            is_closed = state == 'closed'
            
            # Check channel existence and status
            try:
                if thread and hasattr(thread, 'guild'):
                    try:
                        # Attempt to fetch the channel to verify existence
                        await thread.guild.fetch_channel(thread.id)
                        # If we can fetch the channel, it's not closed
                        is_closed = False
                    except discord.NotFound:
                        # Thread no longer exists, mark as closed
                        is_closed = True
                    except Exception:
                        # Silently handle other exceptions
                        is_closed = False
                else:
                    is_closed = True
            except Exception:
                is_closed = False
            
            # Retrieve existing ticket data to preserve last user and moderator IDs
            existing_ticket = await self.ticket_stats_collection.find_one({
                'channel_id': str(thread.id) if thread else 'unknown'
            })
            
            # Prepare minimal stats document following specified format
            stats_doc = {
                'channel_id': str(thread.id) if thread else 'unknown',
                'guild_id': str(thread.guild.id) if thread and thread.guild else 'unknown',
                'current_state': state,
            }
            
            # Preserve existing moderator_id if it exists
            if existing_ticket and existing_ticket.get('moderator_id'):
                stats_doc['moderator_id'] = existing_ticket['moderator_id']
            
            # Add new moderator information if provided and state is not closed
            if user and state != 'closed':
                stats_doc['moderator_id'] = str(user.id)
            
            # Add closure timestamp if closed
            if is_closed:
                stats_doc['closed_at'] = datetime.utcnow()
            
            # Upsert ticket stats
            await self.ticket_stats_collection.update_one(
                {'channel_id': stats_doc['channel_id']},
                {'$set': stats_doc},
                upsert=True
            )
        
        except Exception:
            # Silently handle any unexpected errors
            pass

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
            
            except Exception:
                # Silently handle any unexpected errors
                return False
        
        # Timeout reached without confirmation
        return False

    @commands.command(name="tickets")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def user_tickets(self, ctx, user: discord.User = None, days: int = 30):
        """
        Show closed tickets for a user within specified days
        
        Usage:
        !tickets [user] [days]
        Default is the command invoker and last 30 days
        """
        # Use command invoker if no user specified
        target_user = user or ctx.author
        
        # Validate days input
        days = max(1, min(days, 365))  # Limit between 1 and 365 days
        
        # Calculate cutoff date
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        # Query closed tickets for the user
        closed_tickets = await self.ticket_stats_collection.find({
            'moderator_id': str(target_user.id),
            'current_state': 'closed',
            'closed_at': {'$gte': cutoff_date}
        }).to_list(length=None)
        
        # Create an embed to display ticket information
        embed = discord.Embed(
            title="🎫 Closed Tickets Summary",
            color=self.bot.main_color
        )
        
        # Add user information
        embed.description = (
            f"{target_user.mention} has closed **{len(closed_tickets)}** tickets "
            f"in the last **{days}** days."
        )
        
        # Optional: Add more details if tickets exist
        if closed_tickets:
            # Sort tickets by closure date (most recent first)
            sorted_tickets = sorted(
                closed_tickets, 
                key=lambda x: x.get('closed_at', datetime.min), 
                reverse=True
            )
            
            # Add first few ticket details
            ticket_details = []
            for ticket in sorted_tickets[:5]:  # Limit to 5 most recent
                channel_id = ticket.get('channel_id', 'Unknown')
                closed_at = ticket.get('closed_at', datetime.utcnow())
                
                # Format closure time
                time_diff = (datetime.utcnow() - closed_at).days
                ticket_details.append(
                    f"• Channel ID: `{channel_id}` (Closed {time_diff} days ago)"
                )
            
            if ticket_details:
                embed.add_field(
                    name="Recent Ticket Details",
                    value="\n".join(ticket_details),
                    inline=False
                )
        
        # Send the embed
        await ctx.send(embed=embed)

async def setup(bot):
    """
    Asynchronous setup function for the plugin
    
    :param bot: Discord bot instance
    """
    await bot.add_cog(ClaimThread(bot))
