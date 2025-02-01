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
import aiohttp

from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id


class ClaimThread(commands.Cog):
    """Allows supporters to claim thread by sending claim in the thread channel"""
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self._config_cache = {}
        self._cache_timestamp = 0
        self.channel_cache = {}
        self.user_cache = {}
        self.cache_lifetime = 300  # 5 minutes
        self.check_message_cache = {}
        self.sync_webhook_url = None
        
        # Track command usage per channel
        self.command_usage = {}
        self.reset_times = {}
        
        # Add checks for main commands only
        for cmd_name in ['reply', 'areply', 'freply', 'fareply']:
            if cmd := self.bot.get_command(cmd_name):
                cmd.add_check(check_reply)

        # Add default config with fixed cooldowns
        self.default_config = {
            'limit': 0,
            'override_roles': [],
            'command_cooldown': 5,    # 5 seconds per user
            'thread_cooldown': 300    # 5 minutes per thread
        }

    async def clean_old_claims(self):
        """Clean up claims for non-existent channels"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        async for doc in cursor:
            if 'thread_id' in doc:
                channel = self.bot.get_channel(int(doc['thread_id']))  # Use cache first
                if not channel and ('status' not in doc or doc['status'] != 'closed'):
                    await self.db.find_one_and_update(
                        {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                        {'$set': {'status': 'closed'}}
                    )

    @commands.Cog.listener()
    async def on_ready(self):
        """Clean up old claims when bot starts"""
        await self.clean_old_claims()

    async def get_config(self):
        """Get plugin configuration with defaults"""
        config = await self.db.find_one({'_id': 'config'}) or {}
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
        
        async for doc in self.db.find(
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
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
                except:
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.db.find_one_and_update(
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
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
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
                    await self.db.insert_one({
                        'guild': str(self.bot.modmail_guild.id),
                        'thread_id': str(ctx.thread.channel.id),
                        'claimers': [str(ctx.author.id)],
                        'status': 'open'
                    })
                else:
                    await self.db.find_one_and_update(
                        {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)},
                        {'$set': {'claimers': [str(ctx.author.id)], 'status': 'open'}}
                    )

                embed = discord.Embed(
                    color=self.bot.main_color,
                    description=f"Successfully claimed the thread.\n{ctx.author.mention} is now subscribed to this thread."
                )
                await ctx.send(embed=embed)
                
                # Sync stats after successful claim
                try:
                    stats = await self.get_user_stats(ctx.author.id)
                    await self.send_stats_to_webhook(ctx.author.id, stats)
                except Exception as e:
                    print(f"Stats sync error in claim: {e}")
            except:
                await ctx.message.add_reaction('❌')
        else:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def unclaim(self, ctx):
        """Unclaim a thread without renaming"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            try:
                await self.db.find_one_and_update(
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
                
                # Sync stats after successful unclaim
                try:
                    stats = await self.get_user_stats(ctx.author.id)
                    await self.send_stats_to_webhook(ctx.author.id, stats)
                except Exception as e:
                    print(f"Stats sync error in unclaim: {e}")
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
        cursor = self.db.find({'guild':str(self.bot.modmail_guild.id)})
        active_channels = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(ctx.author.id) in doc['claimers']:
                if 'status' not in doc or doc['status'] != 'closed':
                    try:
                        channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                        if channel:
                            active_channels.append(channel)
                        else:
                            await self.db.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                {'$set': {'status': 'closed'}}
                            )
                    except discord.NotFound:
                        await self.db.find_one_and_update(
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

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="stats")
    async def claim_stats(self, ctx, member: discord.Member = None):
        """View comprehensive claim statistics"""
        target = member or ctx.author
        
        # Get all claims for this user
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
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
                            await self.db.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                {'$set': {'status': 'closed'}}
                            )
                except (discord.NotFound, discord.Forbidden):
                    closed_claims.append(doc)
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
        
        total_claims = len(active_claims) + len(closed_claims)
        
        # Get claim limit
        config = await self.db.find_one({'_id': 'config'})
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
        
        async for doc in self.db.find({
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
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
            except:
                stats['closed'] += 1
                # Update status if there's an error
                if 'status' not in doc or doc['status'] != 'closed':
                    await self.db.find_one_and_update(
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
            
        await self.db.find_one_and_update(
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
            config = await self.db.find_one({'_id': 'config'})
            
            override_roles = []
            for role_id in config.get('override_roles', []):
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
        config = await self.db.find_one({'_id': 'config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id in override_roles:
            return await ctx.send("That role is already an override role")
            
        override_roles.append(role.id)
        await self.db.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Added {role.mention} to override roles")

    @claim_override.command(name='remove')
    async def override_remove(self, ctx, *, role: discord.Role):
        """Remove a role from override claims"""
        config = await self.db.find_one({'_id': 'config'}) or {}
        override_roles = config.get('override_roles', [])
        
        if role.id not in override_roles:
            return await ctx.send("That role is not an override role")
            
        override_roles.remove(role.id)
        await self.db.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'override_roles': override_roles}},
            upsert=True
        )
        
        await ctx.send(f"Removed {role.mention} from override roles")

    async def send_stats_to_webhook(self, user_id, stats):
        """
        Send user's claim statistics to webhook
        
        :param user_id: Discord user ID
        :param stats: Dictionary of claim statistics
        """
        if not self.sync_webhook_url:
            print("Webhook URL not configured")
            return False
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    'user_id': str(user_id),
                    'stats': {
                        'active_claims': stats.get('active_claims', 0),
                        'total_claims': stats.get('total_claims', 0),
                        'timestamp': datetime.now().isoformat()
                    }
                }
                
                print(f"Sending payload: {payload}")  # Debug print
                
                async with session.post(self.sync_webhook_url, json=payload) as response:
                    response_text = await response.text()
                    print(f"Response Status: {response.status}")
                    print(f"Response Text: {response_text}")
                    
                    if response.status == 200:
                        print(f"Successfully sent stats for user {user_id}")
                        return True
                    else:
                        print(f"Failed to send stats. Status: {response.status}, Text: {response_text}")
                        return False
        
        except Exception as e:
            print(f"Webhook sync error: {e}")
            return False

    async def get_user_stats(self, user_id):
        """
        Aggregate claim statistics for a user
        
        :param user_id: Discord user ID
        :return: Dictionary of user's claim statistics
        """
        active_claims = 0
        total_claims = 0
        
        # Query database for user's claims
        cursor = self.db.find({
            'guild': str(self.bot.modmail_guild.id),
            'claimers': str(user_id)
        })
        
        async for doc in cursor:
            total_claims += 1
            
            # Check if claim is active
            if 'status' not in doc or doc['status'] != 'closed':
                active_claims += 1
        
        return {
            'active_claims': active_claims,
            'total_claims': total_claims
        }

    @commands.group(name="claimsync")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def claims_sync_config(self, ctx):
        """Configure claims synchronization webhook"""
        if ctx.invoked_subcommand is None:
            current_url = self.sync_webhook_url or "Not configured"
            embed = discord.Embed(
                title="Claims Sync Webhook",
                description=f"Current Webhook URL: {current_url}",
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)

    @claims_sync_config.command(name="set")
    async def set_sync_webhook(self, ctx, webhook_url: str):
        """Set the webhook URL for claims synchronization"""
        try:
            # Validate webhook URL (optional)
            async with aiohttp.ClientSession() as session:
                async with session.get(webhook_url) as response:
                    if response.status != 200:
                        return await ctx.send("❌ Invalid webhook URL")
            
            # Set webhook URL
            self.sync_webhook_url = webhook_url
            await ctx.send("✅ Webhook URL set successfully!")
        
        except Exception as e:
            await ctx.send(f"❌ Error setting webhook: {e}")

    @commands.command(name="testsync")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def test_sync(self, ctx):
        """Manually trigger a stats sync test"""
        try:
            stats = await self.get_user_stats(ctx.author.id)
            result = await self.send_stats_to_webhook(ctx.author.id, stats)
            
            if result:
                await ctx.send("✅ Test sync successful!")
            else:
                await ctx.send("❌ Test sync failed")
        
        except Exception as e:
            await ctx.send(f"❌ Sync test error: {e}")


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
                
        thread = await cog.db.find_one({
            'thread_id': str(ctx.thread.channel.id), 
            'guild': str(ctx.bot.modmail_guild.id)
        })
        
        # If thread isn't claimed or doesn't exist, allow reply
        if not thread or not thread.get('claimers'):
            return True
            
        # Check for override permissions
        has_override = False
        if config := await cog.db.find_one({'_id': 'config'}):
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


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
