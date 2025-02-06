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

try:
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

load_dotenv()

from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id


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
        channel_id = str(ctx.channel.id)
        
        # Check message cache to prevent spam
        current_time = time.time()
        if channel_id in cog.check_message_cache:
            last_time = cog.check_message_cache[channel_id]
            if current_time - last_time < 5:  # 5 second cooldown
                try:
                    await ctx.message.add_reaction('❌')
                except:
                    pass
                return False
                
        thread = await cog.coll.find_one({
            'thread_id': str(ctx.thread.channel.id), 
            'guild': str(ctx.bot.modmail_guild.id)
        })
        
        # If thread isn't claimed or doesn't exist, allow reply
        if not thread or not thread.get('claimers'):
            return True
            
        # Check for override permissions
        has_override = False
        if config := await cog.coll.find_one({'_id': 'config'}):
            override_roles = config.get('override_roles', [])
            member_roles = [role.id for role in ctx.author.roles]
            has_override = any(role_id in member_roles for role_id in override_roles)
        
        # Allow if user is bot, has override, or is claimer
        can_reply = (
            ctx.author.bot or 
            has_override or 
            str(ctx.author.id) in thread['claimers']
        )
        
        if not can_reply:
            # Update cache, send ephemeral message and add X reaction
            cog.check_message_cache[channel_id] = current_time
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


class ClaimThread(commands.Cog):
    """Allows supporters to claim thread by sending claim in the thread channel"""
    def __init__(self, bot):
        self.bot = bot
        
        # Use bot.api to get plugin partition
        self.coll = bot.api.get_plugin_partition(self)
        
        # Optional: Fallback for pandas-dependent methods
        self.pandas_available = PANDAS_AVAILABLE
        
        self._config_cache = {}
        self._cache_timestamp = 0
        self.channel_cache = {}
        self.user_cache = {}
        self.cache_lifetime = 300  # 5 minutes
        self.check_message_cache = {}
        
        # Webhook configuration
        self.ticket_export_webhook = None
        
        # Track command usage per channel
        self.command_usage = {}
        self.reset_times = {}
        
        # Default configuration
        self.default_config = {
            'limit': 5,  # Default claim limit
            'override_roles': [],  # Default override roles
            'command_cooldown': 5,    # 5 seconds per user
            'thread_cooldown': 300    # 5 minutes per thread
        }

    async def clean_old_claims(self):
        """Clean up claims for non-existent channels"""
        cursor = self.coll.find({'guild': str(self.bot.modmail_guild.id)})
        async for doc in cursor:
            if 'thread_id' in doc:
                channel = self.bot.get_channel(int(doc['thread_id']))  # Use cache first
                if not channel and ('status' not in doc or doc['status'] != 'closed'):
                    await self.coll.find_one_and_update(
                        {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                        {'$set': {'status': 'closed'}}
                    )

    @commands.Cog.listener()
    async def on_ready(self):
        """Clean up old claims when bot starts"""
        await self.clean_old_claims()

    async def get_config(self):
        """Get plugin configuration with defaults"""
        config = await self.coll.find_one({'_id': 'config'}) or {}
        return {
            'limit': config.get('limit', self.default_config['limit']),
            'override_roles': config.get('override_roles', self.default_config['override_roles']),
            'command_cooldown': config.get('command_cooldown', self.default_config['command_cooldown']),
            'thread_cooldown': config.get('thread_cooldown', self.default_config['thread_cooldown'])
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
        
        async for doc in self.coll.find(
            {
                'guild': str(self.bot.modmail_guild.id),
                'claimers': {'$exists': True}
            }
        ):
            # Skip if before cutoff date
            if cutoff_date and 'created_at' in doc:
                created_at = datetime.fromisoformat(doc['created_at'])
                if created_at < cutoff_date:
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
                        await self.coll.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
                except:
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.coll.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
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
    async def claim(self, ctx):
        """Claim a thread without renaming"""
        thread = await self.coll.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
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
                    await self.coll.insert_one({
                        'guild': str(self.bot.modmail_guild.id),
                        'thread_id': str(ctx.thread.channel.id),
                        'claimers': [str(ctx.author.id)],
                        'status': 'open'
                    })
                else:
                    await self.coll.find_one_and_update(
                        {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)},
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
    async def unclaim(self, ctx):
        """Unclaim a thread without renaming"""
        thread = await self.coll.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            try:
                await self.coll.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
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
    async def claims(self, ctx):
        """Check which channels you have claimed"""
        cursor = self.coll.find({'guild':str(self.bot.modmail_guild.id)})
        active_channels = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(ctx.author.id) in doc['claimers']:
                if 'status' not in doc or doc['status'] != 'closed':
                    try:
                        channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                        if channel:
                            active_channels.append(channel)
                        else:
                            await self.coll.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                {'$set': {'status': 'closed'}}
                            )
                    except discord.NotFound:
                        await self.coll.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
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
        cursor = self.coll.find({'guild': str(self.bot.modmail_guild.id)})
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
                            await self.coll.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                {'$set': {'status': 'closed'}}
                            )
                except (discord.NotFound, discord.Forbidden):
                    closed_claims.append(doc)
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.coll.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
        
        total_claims = len(active_claims) + len(closed_claims)
        
        # Get claim limit
        config = await self.get_config()
        limit = config.get('limit', 0) if config else 0
        
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
        
        async for doc in self.coll.find({
            'guild': str(self.bot.modmail_guild.id),
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
                        await self.coll.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
            except:
                stats['closed'] += 1
                # Update status if there's an error
                if 'status' not in doc or doc['status'] != 'closed':
                    await self.coll.find_one_and_update(
                        {'thread_id': doc['thread_id'], 'guild': doc['guild']},
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
                description=f"Current claim limit: **{config['limit']}**\n"
                          f"Use `{ctx.prefix}claimconfig limit <number>` to change",
                color=self.bot.main_color
            )
            await ctx.send(embed=embed)

    @claim_config.command(name='limit')
    async def set_claim_limit(self, ctx, limit: int):
        """Set the maximum number of claims per user (0 for unlimited)"""
        if limit < 0:
            return await ctx.send("Limit cannot be negative")
            
        await self.coll.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'limit': limit}},
            upsert=True
        )
        
        limit_text = str(limit) if limit > 0 else "unlimited"
        await ctx.send(f"Claim limit set to {limit_text}")

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.group(name='override', invoke_without_command=True)
    async def claim_override(self, ctx):
        """Manage override roles for claims"""
        if ctx.invoked_subcommand is None:
            config = await self.coll.find_one({'_id': 'config'})
            
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
        config = await self.coll.find_one({'_id': 'config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id in override_roles:
            return await ctx.send("That role is already an override role")
            
        override_roles.append(role.id)
        await self.coll.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Added {role.mention} to override roles")

    @claim_override.command(name='remove')
    async def override_remove(self, ctx, *, role: discord.Role):
        """Remove a role from override claims"""
        config = await self.coll.find_one({'_id': 'config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id not in override_roles:
            return await ctx.send("That role is not an override role")
            
        override_roles.remove(role.id)
        await self.coll.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Removed {role.mention} from override roles")

    async def export_claimed_tickets(self, days=None):
        """
        Export claimed tickets to a file
        
        :param days: Number of days to look back
        :return: Filename or CSV content
        """
        # Prepare query
        query = {
            'guild': str(self.bot.modmail_guild.id),
            'claimers': {'$exists': True}
        }
        
        # Add date filter if days specified
        if days:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            query['claimed_at'] = {'$gte': cutoff_date}
        
        ticket_data = []
        
        # Fetch tickets
        async for doc in self.coll.find(query):
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
            config_collection = self.coll
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

    async def cog_load(self):
        """
        Load saved configuration when the cog is loaded
        """
        try:
            # Try to load previously saved webhook URL
            config_collection = self.coll
            config = await config_collection.find_one({'_id': 'ticket_export_config'})
            
            if config and 'webhook_url' in config:
                self.ticket_export_webhook = config['webhook_url']
        except Exception as e:
            print(f"Error loading ticket export config: {e}")

    async def cog_unload(self):
        """
        Close MongoDB connection when cog is unloaded
        """
        if hasattr(self, 'coll'):
            pass

    async def update_ticket_stats(self, user_id):
        """
        Update ticket stats for a user in shared MongoDB and trigger webhook
        """
        # Find and update user stats atomically
        result = await self.coll.find_one_and_update(
            {'user_id': user_id},
            {'$inc': {'closed_tickets': 1}},
            upsert=True,
            return_document=True
        )
        
        # Extract updated ticket count
        closed_tickets = result.get('closed_tickets', 1) if result else 1
        
        # Send via webhook from environment variable
        webhook_url = os.getenv('TICKET_STATS_WEBHOOK_URL')
        if webhook_url:
            try:
                async with aiohttp.ClientSession() as session:
                    stats_payload = {
                        'user_id': user_id,
                        'closed_tickets': closed_tickets
                    }
                    
                    async with session.post(
                        webhook_url, 
                        json=stats_payload
                    ) as response:
                        if response.status == 200:
                            print(f"Successfully exported claims to {filename}")
                            return filename
                        else:
                            print(f"Failed to send webhook: {response.status}")
                            return None
            except Exception as e:
                print(f"Error sending webhook: {e}")
                return None

    @commands.command(name="show_ticket_stats")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def show_ticket_stats(self, ctx, user_id: int = None):
        """
        Show ticket stats for a user or all users
        """
        if user_id:
            # Fetch stats for specific user
            user_stats = await self.coll.find_one({'user_id': user_id})
            
            if user_stats:
                await ctx.send(f"User {user_id} has closed {user_stats.get('closed_tickets', 0)} tickets.")
            else:
                await ctx.send(f"No ticket stats found for user {user_id}.")
        else:
            # Fetch all user stats
            all_stats = await self.coll.find().to_list(length=None)
            
            if all_stats:
                stats_message = "Ticket Stats:\n"
                for stat in all_stats:
                    stats_message += f"User {stat['user_id']}: {stat['closed_tickets']} tickets\n"
                await ctx.send(stats_message)
            else:
                await ctx.send("No ticket stats available.")

    @commands.Cog.listener()
    async def on_thread_close(self, thread):
        """
        Automatically update ticket stats when a thread is closed
        """
        # Assuming thread object has a closer attribute with user ID
        if hasattr(thread, 'closer') and thread.closer:
            closer_id = thread.closer.id
            closed_count = await self.update_ticket_stats(closer_id)
            
            # Optional: Log or notify about ticket closure
            print(f"User {closer_id} closed a ticket. Total closed tickets: {closed_count}")

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
