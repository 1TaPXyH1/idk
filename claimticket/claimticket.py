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
            config_exists = await self.config_collection.find_one({'_id': 'config'})
            if not config_exists:
                await self.config_collection.insert_one({
                    '_id': 'config',
                    'limit': 5,
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
            'limit': 5,  # Default claim limit
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
                                    # Fetch user and get ticket closure stats
                                    user = await self.bot.fetch_user(int(ticket['moderator_id']))
                                    stats = await self.get_ticket_closure_stats(ticket['moderator_id'])
                                    
                                    # Construct personalized message
                                    message = (
                                        f"Congrats on closing your {stats['daily_tickets']} ticket of the day! "
                                        f"This is your {stats['monthly_tickets']} ticket of the month."
                                    )
                                    
                                    await user.send(message)
                                    
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
        """Get plugin configuration with defaults"""
        config = await self.ticket_stats_collection.find_one({'_id': 'config'}) or {}
        return {
            'limit': config.get('limit', 0),
            'override_roles': config.get('override_roles', []),
            'command_cooldown': config.get('command_cooldown', 5),
            'thread_cooldown': config.get('thread_cooldown', 300)
        }

    async def check_claimer(self, ctx, claimer_id):
        """
        Check if user can claim more threads based on configuration
        
        :param ctx: Command context
        :param claimer_id: ID of the user trying to claim
        :return: Boolean indicating if user can claim
        """
        config = await self.get_config()
        limit = config.get('limit', 0)
        
        if limit == 0:  # Unlimited claims
            return True
        
        # Count active claims for this user
        active_claims = await self.ticket_stats_collection.count_documents({
            'guild': str(self.bot.modmail_guild.id),
            'claimers': str(claimer_id),
            'status': {'$ne': 'closed'}
        })
        
        return active_claims < limit

    @commands.command(name="claim", aliases=["c"])
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def claim_thread(self, ctx):
        """Claim the current ticket thread"""
        # Check if thread is already claimed
        thread = await self.ticket_stats_collection.find_one({
            'thread_id': str(ctx.channel.id), 
            'guild': str(self.bot.modmail_guild.id)
        })
        
        # Check if thread has active claimers
        has_active_claimers = thread and thread.get('claimers') and len(thread['claimers']) > 0
        
        # If thread is already claimed and current user is not a claimer
        if has_active_claimers and (not thread or str(ctx.author.id) not in thread.get('claimers', [])):
            embed = discord.Embed(
                description="This thread is already claimed by another user.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)
            await ctx.message.add_reaction('🚫')
            return

        # Proceed with claiming if not already claimed or is current claimer
        try:
            # Check claim limits
            if not await self.check_claimer(ctx, ctx.author.id):
                await ctx.message.add_reaction('🚫')
                return

            # Update database
            if thread is None:
                await self.ticket_stats_collection.insert_one({
                    'guild': str(self.bot.modmail_guild.id),
                    'thread_id': str(ctx.thread.channel.id),
                    'claimers': [str(ctx.author.id)],
                    'status': 'open'
                })
            else:
                await self.ticket_stats_collection.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)},
                    {'$set': {'claimers': [str(ctx.author.id)], 'status': 'open'}}
                )

            # Delete the claim command message
            try:
                await ctx.message.delete()
            except:
                pass

            # Send claim embed
            embed = discord.Embed(
                title="📋 Ticket Claimed",
                description=f"{ctx.author.mention} claimed the ticket.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.message.add_reaction('❌')
            print(f"Claim error: {e}")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def unclaim_thread(self, ctx):
        """Unclaim the current ticket thread"""
        thread = await self.ticket_stats_collection.find_one({
            'thread_id': str(ctx.thread.channel.id), 
            'guild': str(self.bot.modmail_guild.id)
        })
        
        # Check if thread is claimed and current user is a claimer or moderator
        if not thread or not thread.get('claimers'):
            embed = discord.Embed(
                description="This thread is not currently claimed.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)
            await ctx.message.add_reaction('🚫')
            return

        # Only allow unclaim if user is a claimer or moderator
        if (await self.is_moderator(ctx.author)) or str(ctx.author.id) in thread['claimers']:
            try:
                # Remove the claimer
                await self.ticket_stats_collection.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
                    {'$pull': {'claimers': str(ctx.author.id)}}
                )
                
                # Delete the unclaim command message
                try:
                    await ctx.message.delete()
                except:
                    pass

                # Send unclaim embed
                embed = discord.Embed(
                    title="🔓 Ticket Unclaimed",
                    description=f"{ctx.author.mention} unclaimed the ticket.",
                    color=discord.Color.dark_orange()
                )
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.message.add_reaction('❌')
                print(f"Unclaim error: {e}")
        else:
            embed = discord.Embed(
                description="You are not authorized to unclaim this thread.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)
            await ctx.message.add_reaction('🚫')

    @commands.command(name="thread_notify", aliases=["tn", "n"])
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_notify(self, ctx):
        """Toggle thread notifications"""
        # Ensure thread exists in config
        thread = await self.ticket_stats_collection.find_one({
            'thread_id': str(ctx.thread.channel.id), 
            'guild': str(self.bot.modmail_guild.id)
        })

        # Create thread record if not exists
        if thread is None:
            await self.ticket_stats_collection.insert_one({
                'thread_id': str(ctx.thread.channel.id), 
                'guild': str(self.bot.modmail_guild.id),
                'subscriptions': [str(ctx.author.id)]
            })
            
            # Update config subscriptions
            if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                self.bot.config["subscriptions"][str(ctx.thread.id)] = []
            
            if ctx.author.mention not in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                self.bot.config["subscriptions"][str(ctx.thread.id)].append(ctx.author.mention)
                await self.bot.config.update()
            
            embed = discord.Embed(
                title="🔔 Thread Notifications",
                description=f"{ctx.author.mention} subscribed to thread notifications.",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            return

        # Get current subscriptions
        subscriptions = thread.get('subscriptions', [])
        
        # Toggle subscription
        if str(ctx.author.id) in subscriptions:
            subscriptions.remove(str(ctx.author.id))
            
            # Remove from config subscriptions
            if str(ctx.thread.id) in self.bot.config["subscriptions"]:
                if ctx.author.mention in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                    self.bot.config["subscriptions"][str(ctx.thread.id)].remove(ctx.author.mention)
                    await self.bot.config.update()
            
            embed = discord.Embed(
                title="🔔 Thread Notifications",
                description=f"{ctx.author.mention} unsubscribed from thread notifications.",
                color=discord.Color.red()
            )
        else:
            subscriptions.append(str(ctx.author.id))
            
            # Add to config subscriptions
            if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                self.bot.config["subscriptions"][str(ctx.thread.id)] = []
            
            if ctx.author.mention not in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                self.bot.config["subscriptions"][str(ctx.thread.id)].append(ctx.author.mention)
                await self.bot.config.update()
            
            embed = discord.Embed(
                title="🔔 Thread Notifications",
                description=f"{ctx.author.mention} subscribed to thread notifications.",
                color=discord.Color.green()
            )

        # Update thread record
        await self.ticket_stats_collection.find_one_and_update(
            {
                'thread_id': str(ctx.thread.channel.id), 
                'guild': str(self.bot.modmail_guild.id)
            }, 
            {'$set': {'subscriptions': subscriptions}}
        )

        await ctx.send(embed=embed)

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
                        # Attempt to fetch the thread to verify existence
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

    async def get_ticket_closure_stats(self, moderator_id):
        """
        Calculate daily and monthly ticket closure statistics
        
        :param moderator_id: ID of the moderator
        :return: Dictionary with daily and monthly ticket closure counts
        """
        # Get current time
        now = datetime.utcnow()
        
        # Calculate start of today and start of this month
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Convert moderator_id to string for database query
        str_moderator_id = str(moderator_id)
        
        # Count daily closed tickets
        daily_tickets = await self.ticket_stats_collection.count_documents({
            'moderator_id': str_moderator_id,
            'current_state': 'closed',
            'closed_at': {'$gte': today_start}
        })
        
        # Count monthly closed tickets
        monthly_tickets = await self.ticket_stats_collection.count_documents({
            'moderator_id': str_moderator_id,
            'current_state': 'closed',
            'closed_at': {'$gte': month_start}
        })
        
        return {
            'daily_tickets': daily_tickets,
            'monthly_tickets': monthly_tickets
        }

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

async def check_reply(ctx):
    """Check if user can reply to the thread"""
    # Skip check if not a reply command
    reply_commands = ['reply', 'areply', 'freply', 'fareply']
    if ctx.command.name not in reply_commands:
        return True
    
    # Skip check if no thread attribute
    if not hasattr(ctx, 'thread'):
        return True

    try:
        cog = ctx.bot.get_cog('ClaimThread')
        
        # Check if user is a moderator (full bypass)
        if await cog.is_moderator(ctx.author):
            return True

        # Find the thread record
        thread = await cog.ticket_stats_collection.find_one({
            'thread_id': str(ctx.thread.channel.id), 
            'guild': str(ctx.bot.modmail_guild.id)
        })
        
        # If thread isn't claimed, allow reply
        if not thread or not thread.get('claimers'):
            return True
        
        # Check for override permissions
        config = await cog.get_config()
        override_roles = config.get('override_roles', [])
        member_roles = [role.id for role in ctx.author.roles]
        has_override = any(role_id in member_roles for role_id in override_roles)
        
        # Allow if user has override or is claimer
        can_reply = (
            has_override or 
            str(ctx.author.id) in thread['claimers']
        )
        
        if not can_reply:
            try:
                embed = discord.Embed(
                    description="This thread has been claimed by another user.",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed, ephemeral=True)
                await ctx.message.add_reaction('❌')
            except:
                pass
            return False
            
        return True
        
    except Exception as e:
        print(f"Error in check_reply: {e}")
        return True

async def setup(bot):
    """
    Asynchronous setup function for the plugin
    
    :param bot: Discord bot instance
    """
    await bot.add_cog(ClaimThread(bot))
