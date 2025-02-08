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
    Check if the command is being used in a valid thread channel
    
    Args:
        ctx: The command context
    
    Returns:
        bool: True if in a valid thread, False otherwise
    """
    # Check if channel is a thread
    if not isinstance(ctx.channel, discord.Thread):
        # Check if it's in the specific category
        if ctx.channel.category_id == 1334667715444473886:
            return True
        
        # Send an embed for invalid channel
        embed = discord.Embed(
            title="❌ Invalid Channel",
            description="This command can only be used in ModMail threads or the specified category.",
            color=discord.Color.red()
        )
        ctx.bot.loop.create_task(ctx.send(embed=embed))
        return False
    
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

        # Add checks for main commands
        for cmd_name in ['reply', 'areply', 'freply', 'fareply', 'close', 'claim', 'unclaim']:
            if cmd := self.bot.get_command(cmd_name):
                if cmd_name in ['reply', 'areply', 'freply', 'fareply', 'close']:
                    cmd.add_check(check_reply)
                elif cmd_name == 'claim':
                    cmd.add_check(check_claim)
                elif cmd_name == 'unclaim':
                    cmd.add_check(check_unclaim)

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
                    'status': {'$ne': 'closed'}
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
                                    
                                    # Mark this ticket as notified
                                    self.notified_closed_tickets.add(channel_id)
                                except Exception as dm_error:
                                    print(f"Failed to process ticket {ticket.get('_id')}: {dm_error}")
                            
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
            'guild_id': str(self.bot.modmail_guild.id),
            'moderator_id': str(claimer_id),
            'status': {'$ne': 'closed'}
        })
        
        return active_claims < limit

    @commands.command(name="claim", aliases=["c"])
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def claim_thread(self, ctx):
        """Claim the current ticket thread"""
        try:
            # Check if thread is already claimed
            thread_claim = await self.ticket_stats_collection.find_one({
                'guild_id': str(ctx.guild.id),
                'channel_id': str(ctx.channel.id),
                'status': 'claimed'
            })
            
            # If thread is claimed
            if thread_claim:
                # Check for override permissions
                has_override = False
                config = await self.config_collection.find_one({'_id': 'config'})
                if config:
                    override_roles = config.get('override_roles', [])
                    member_roles = [role.id for role in ctx.author.roles]
                    has_override = any(role_id in member_roles for role_id in override_roles)
                
                # If not an override role, block claim
                if not has_override:
                    await ctx.message.add_reaction('🚫')
                    return

            # Proceed with claiming
            await self.ticket_stats_collection.update_one(
                {
                    'guild_id': str(ctx.guild.id),
                    'channel_id': str(ctx.channel.id)
                },
                {
                    '$set': {
                        'moderator_id': str(ctx.author.id),
                        'status': 'claimed'
                    }
                },
                upsert=True
            )

            # Send claim embed
            embed = discord.Embed(
                title="Ticket Claimed",
                description=f"{ctx.author.mention} claimed the ticket.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
        
        except Exception as e:
            await ctx.message.add_reaction('🚫')
            print(f"Claim error: {e}")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def unclaim_thread(self, ctx):
        """Unclaim the current ticket thread"""
        try:
            # Update ticket status to unclaimed
            result = await self.ticket_stats_collection.update_one(
                {
                    'guild_id': str(ctx.guild.id),
                    'channel_id': str(ctx.channel.id),
                    'moderator_id': str(ctx.author.id)
                },
                {
                    '$set': {
                        'status': 'unclaimed',
                        'moderator_id': None
                    }
                }
            )

            if result.modified_count > 0:
                embed = discord.Embed(
                    title="🔓 Ticket Unclaimed",
                    description=f"{ctx.author.mention} unclaimed the ticket.",
                    color=discord.Color.dark_orange()
                )
                await ctx.send(embed=embed)
            else:
                await ctx.message.add_reaction('🚫')
        
        except Exception as e:
            await ctx.message.add_reaction('❌')
            print(f"Unclaim error: {e}")

    @commands.command(name="ticket_close", aliases=["tclose"])
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def close_thread(self, ctx, *, reason=None):
        """Close the current ticket thread"""
        try:
            # Update ticket status to closed
            await self.ticket_stats_collection.update_one(
                {
                    'guild_id': str(ctx.guild.id),
                    'channel_id': str(ctx.channel.id)
                },
                {
                    '$set': {
                        'status': 'closed',  
                        'moderator_id': str(ctx.author.id),
                        'closed_at': datetime.utcnow()
                    }
                },
                upsert=True
            )

            # Your existing close logic here
            # ... (rest of the close method remains the same)
        
        except Exception as e:
            await ctx.message.add_reaction('❌')
            print(f"Close error: {e}")

    @commands.command(name="rename")
    @commands.check(is_in_thread)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def rename_thread(self, ctx, *, new_name: str):
        """Rename the current ticket thread"""
        try:
            # Validate thread name
            if len(new_name) > 100:
                await ctx.message.add_reaction('❌')
                return await ctx.send("Thread name cannot exceed 100 characters.")
            
            # Attempt to rename using different methods
            try:
                # First, try using thread's channel
                await ctx.thread.channel.edit(name=new_name)
            except AttributeError:
                try:
                    # If channel method fails, try direct edit
                    await ctx.thread.edit(name=new_name)
                except Exception as e:
                    # If all else fails, send a message
                    await ctx.message.add_reaction('❌')
                    return await ctx.send(f"Could not rename thread: {str(e)}")
            
            await ctx.message.add_reaction('✅')
            
        except discord.Forbidden:
            await ctx.message.add_reaction('❌')
            await ctx.send("I don't have permission to rename this thread.")
        except Exception as e:
            await ctx.message.add_reaction('❌')
            print(f"Rename error: {e}")

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
                'status': 'closed' if is_closed else 'open',
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
                'status': state,
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


    @commands.command(name="tickets")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def tickets_command(self, ctx, user: discord.User = None, days: int = 7):
        """
        Show ticket statistics for a user
        
        :param ctx: Command context
        :param user: User to check tickets for (defaults to command invoker)
        :param days: Number of days to look back (default 7)
        """
        try:
            # Default to command invoker if no user specified
            if user is None:
                user = ctx.author
            
            # Validate days input
            days = max(1, min(days, 365))  # Limit between 1 and 365 days
            
            # Calculate start date
            start_date = datetime.utcnow() - timedelta(days=days)
            
            # Aggregate pipeline to count closed tickets
            ticket_pipeline = [
                {
                    '$match': {
                        'moderator_id': str(user.id),
                        'status': 'closed',
                        'closed_at': {'$gte': start_date}
                    }
                },
                {'$count': 'ticket_count'}
            ]
            
            # Execute aggregation
            ticket_result = await self.ticket_stats_collection.aggregate(ticket_pipeline).to_list(length=1)
            
            # Extract ticket count
            ticket_count = ticket_result[0]['ticket_count'] if ticket_result else 0
            
            # Create embed
            embed = discord.Embed(
                title="Ticket Statistics",
                description=(
                    f"{user.mention} has {ticket_count} ticket{'s' if ticket_count != 1 else ''} "
                    f"in the past {days} day{'s' if days != 1 else ''}."
                ),
                color=discord.Color.blue()
            )
            
            # Add user avatar
            embed.set_thumbnail(url=user.display_avatar.url)
            
            # Send embed
            await ctx.send(embed=embed)
        
        except Exception as e:
            # Error handling
            error_embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to retrieve ticket statistics: {str(e)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=error_embed)
            print(f"Tickets command error: {e}")

    @commands.group(name="claimconfig", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def claim_config(self, ctx):
        """Configure claim override settings"""
        # Retrieve current configuration
        config = await self.ticket_stats_collection.find_one({'_id': 'config'})
        override_roles = config.get('override_roles', []) if config else []
        
        # Create embed to show current override roles
        embed = discord.Embed(
            title="Claim Override Configuration",
            description="Roles that can bypass claim restrictions",
            color=self.bot.main_color
        )
        
        if override_roles:
            role_mentions = []
            for role_id in override_roles:
                role = ctx.guild.get_role(role_id)
                if role:
                    role_mentions.append(role.mention)
            
            embed.add_field(
                name="Current Override Roles", 
                value="\n".join(role_mentions) if role_mentions else "No roles configured",
                inline=False
            )
        else:
            embed.description = "No override roles configured"
        
        embed.set_footer(text="Use .claimconfig add/remove @Role to modify")
        await ctx.send(embed=embed)

    @claim_config.command(name="add")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def claim_override_add(self, ctx, *, role: discord.Role):
        """Add a role to claim override list"""
        try:
            # Retrieve or create config
            config = await self.ticket_stats_collection.find_one({'_id': 'config'}) or {}
            
            # Get current override roles or initialize empty list
            override_roles = config.get('override_roles', [])
            
            # Check if role is already in override list
            if role.id in override_roles:
                return await ctx.send(f"{role.mention} is already in the override list.")
            
            # Add role to override list
            override_roles.append(role.id)
            
            # Update configuration
            await self.config_collection.update_one(
                {'_id': 'config'},
                {'$set': {'override_roles': override_roles}},
                upsert=True
            )
            
            # Create confirmation embed
            embed = discord.Embed(
                title="Claim Override Role Added",
                description=f"{role.mention} can now bypass claim restrictions",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        
        except Exception as e:
            await ctx.send(f"Error adding override role: {str(e)}")

    @claim_config.command(name="remove")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def claim_override_remove(self, ctx, *, role: discord.Role):
        """Remove a role from claim override list"""
        try:
            # Retrieve configuration
            config = await self.ticket_stats_collection.find_one({'_id': 'config'})
            
            # Get current override roles
            override_roles = config.get('override_roles', [])
            
            # Check if role is in override list
            if role.id not in override_roles:
                return await ctx.send(f"{role.mention} is not in the override list.")
            
            # Remove role from override list
            override_roles.remove(role.id)
            
            # Update configuration
            await self.config_collection.update_one(
                {'_id': 'config'},
                {'$set': {'override_roles': override_roles}},
                upsert=True
            )
            
            # Create confirmation embed
            embed = discord.Embed(
                title="Claim Override Role Removed",
                description=f"{role.mention} can no longer bypass claim restrictions",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        
        except Exception as e:
            await ctx.send(f"Error removing override role: {str(e)}")

    @commands.command(name="transfer")
    @commands.check(is_in_thread)
    async def transfer_claim(self, ctx, member: discord.Member):
        """Transfer ticket claim to another user (only by override roles)"""
        try:
            # Check if user has override permissions
            has_override = False
            config = await self.config_collection.find_one({'_id': 'config'})
            if config:
                override_roles = config.get('override_roles', [])
                member_roles = [role.id for role in ctx.author.roles]
                has_override = any(role_id in member_roles for role_id in override_roles)
            
            # Only allow transfer by override roles
            if not has_override:
                return await ctx.send("Only users with override roles can transfer claims.")
            
            # Check current claim status
            channel_id = str(ctx.thread.channel.id)
            current_claim = await self.ticket_stats_collection.find_one({
                'guild_id': str(ctx.guild.id),
                'channel_id': channel_id,
                'status': 'claimed'
            })
            
            if not current_claim:
                return await ctx.send("This ticket is not currently claimed.")
            
            # Update claim to new user
            await self.ticket_stats_collection.update_one(
                {
                    'guild_id': str(ctx.guild.id),
                    'channel_id': channel_id
                },
                {
                    '$set': {
                        'moderator_id': str(member.id),
                        'transferred_by': str(ctx.author.id)
                    }
                }
            )
            
            # Create transfer embed
            embed = discord.Embed(
                title="Ticket Claim Transferred",
                description=f"Ticket claim transferred from {ctx.author.mention} to {member.mention}",
                color=self.bot.main_color
            )
            embed.add_field(name="Original Claimer", value=ctx.author.mention, inline=True)
            embed.add_field(name="New Claimer", value=member.mention, inline=True)
            
            await ctx.send(embed=embed)
        
        except Exception as e:
            await ctx.send(f"Error transferring claim: {str(e)}")

async def check_reply(ctx):
    """Check if user can reply to the thread"""
    # Skip check if not a reply or close command
    reply_and_close_commands = ['reply', 'areply', 'freply', 'fareply', 'close']
    if ctx.command.name not in reply_and_close_commands:
        return True
    
    # Skip check if no thread attribute
    if not hasattr(ctx, 'thread'):
        return True

    try:
        cog = ctx.bot.get_cog('ClaimThread')
        channel_id = str(ctx.thread.channel.id)
        
        # Check message cache to prevent spam
        current_time = time.time()
        if channel_id in cog.check_message_cache:
            last_time = cog.check_message_cache[channel_id]
            if current_time - last_time < 5:  # 5 second cooldown
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
                
        # Check if thread is claimed
        thread_claim = await cog.ticket_stats_collection.find_one({
            'guild_id': str(ctx.guild.id),
            'channel_id': channel_id,
            'status': 'claimed'
        })
        
        # If thread is claimed
        if thread_claim:
            # Check for override permissions
            has_override = False
            config = await cog.config_collection.find_one({'_id': 'config'})
            if config:
                override_roles = config.get('override_roles', [])
                member_roles = [role.id for role in ctx.author.roles]
                has_override = any(role_id in member_roles for role_id in override_roles)
            
            # Allow reply/close if:
            # 1. User is the moderator who claimed the ticket
            # 2. User has override roles
            # 3. User is a bot
            can_interact = (
                ctx.author.bot or 
                str(ctx.author.id) == thread_claim.get('moderator_id') or 
                has_override
            )
            
            if not can_interact:
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
            
        return True
        
    except Exception as e:
        print(f"Error in check_reply: {e}")
        return True

async def check_claim(ctx):
    """Check if thread can be claimed"""
    # Skip check if no thread attribute
    if not hasattr(ctx, 'thread'):
        return True

    try:
        cog = ctx.bot.get_cog('ClaimThread')
        channel_id = str(ctx.thread.channel.id)
        
        # Check message cache to prevent spam
        current_time = time.time()
        if channel_id in cog.check_message_cache:
            last_time = cog.check_message_cache[channel_id]
            if current_time - last_time < 5:  # 5 second cooldown
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
                
        # Check if thread is already claimed
        thread_claim = await cog.ticket_stats_collection.find_one({
            'guild_id': str(ctx.guild.id),
            'channel_id': channel_id,
            'status': 'claimed'
        })
        
        # If thread is claimed
        if thread_claim:
            # Check for override permissions
            has_override = False
            config = await cog.config_collection.find_one({'_id': 'config'})
            if config:
                override_roles = config.get('override_roles', [])
                member_roles = [role.id for role in ctx.author.roles]
                has_override = any(role_id in member_roles for role_id in override_roles)
            
            # If not an override role, block claim
            if not has_override:
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
            
        return True
        
    except Exception as e:
        print(f"Error in check_claim: {e}")
        return True

async def check_unclaim(ctx):
    """Check if thread can be unclaimed"""
    # Skip check if no thread attribute
    if not hasattr(ctx, 'thread'):
        return True

    try:
        cog = ctx.bot.get_cog('ClaimThread')
        channel_id = str(ctx.thread.channel.id)
        
        # Check message cache to prevent spam
        current_time = time.time()
        if channel_id in cog.check_message_cache:
            last_time = cog.check_message_cache[channel_id]
            if current_time - last_time < 5:  # 5 second cooldown
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
                
        # Check if thread is claimed
        thread_claim = await cog.ticket_stats_collection.find_one({
            'guild_id': str(ctx.guild.id),
            'channel_id': channel_id,
            'status': 'claimed'
        })
        
        # If thread is claimed
        if thread_claim:
            # Check for override permissions
            has_override = False
            config = await cog.config_collection.find_one({'_id': 'config'})
            if config:
                override_roles = config.get('override_roles', [])
                member_roles = [role.id for role in ctx.author.roles]
                has_override = any(role_id in member_roles for role_id in override_roles)
            
            # Only allow unclaim if:
            # 1. User is the moderator who claimed the ticket
            # 2. User has override roles
            can_unclaim = (
                str(ctx.author.id) == thread_claim.get('moderator_id') or 
                has_override
            )
            
            if not can_unclaim:
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
            
        return True
        
    except Exception as e:
        print(f"Error in check_unclaim: {e}")
        return True

async def check_close(ctx):
    """Check if user can close the thread"""
    # Skip check if no thread attribute
    if not hasattr(ctx, 'thread'):
        return True

    try:
        cog = ctx.bot.get_cog('ClaimThread')
        channel_id = str(ctx.thread.channel.id)
        
        # Check message cache to prevent spam
        current_time = time.time()
        if channel_id in cog.check_message_cache:
            last_time = cog.check_message_cache[channel_id]
            if current_time - last_time < 5:  # 5 second cooldown
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
                
        # Check if thread is claimed
        thread_claim = await cog.ticket_stats_collection.find_one({
            'guild_id': str(ctx.guild.id),
            'channel_id': channel_id,
            'status': 'claimed'
        })
        
        # If thread is claimed
        if thread_claim:
            # Check for override permissions
            has_override = False
            config = await cog.config_collection.find_one({'_id': 'config'})
            if config:
                override_roles = config.get('override_roles', [])
                member_roles = [role.id for role in ctx.author.roles]
                has_override = any(role_id in member_roles for role_id in override_roles)
            
            # Allow close if:
            # 1. User is the moderator who claimed the ticket
            # 2. User has override roles
            # 3. User is a bot
            can_close = (
                ctx.author.bot or 
                str(ctx.author.id) == thread_claim.get('moderator_id') or 
                has_override
            )
            
            if not can_close:
                cog.check_message_cache[channel_id] = current_time
                await ctx.message.add_reaction('🚫')
                return False
            
        return True
        
    except Exception as e:
        print(f"Error in check_close: {e}")
        return True

async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
