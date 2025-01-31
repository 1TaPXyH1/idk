# Credits and orignal author: https://github.com/fourjr/modmail-plugins/blob/master/claim/claim.py
# Slightly modified for Minion_Kadin#2022 (discord)
# Please use the original plugin as this one may cause your bot to nuke the world

import discord
from discord.ext import commands
import time
import asyncio

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
        self.thread_cooldowns = {}
        check_reply.fail_msg = 'This thread has been claimed by another user.'
        self.bot.get_command('reply').add_check(check_reply)
        self.bot.get_command('areply').add_check(check_reply)
        self.bot.get_command('fareply').add_check(check_reply)
        self.bot.get_command('freply').add_check(check_reply)
        
        # Add default config
        self.default_config = {
            'limit': 0,
            'bypass_roles': [],
            'override_roles': [],
            'command_cooldown': 60, # Default 60 second cooldown
            'thread_cooldown': 300  # Default 5 minute thread cooldown
        }

    async def check_claimer(self, ctx, claimer_id):
        """
        Check if a user can claim more threads
        
        Parameters
        ----------
        ctx : Context
            The command context
        claimer_id : int
            The ID of the user attempting to claim
            
        Returns
        -------
        bool
            True if user can claim more threads, False otherwise
        """
        config = await self.db.find_one({'_id': 'config'})
        if config and 'limit' in config:
            if config['limit'] == 0:
                return True
        else:
            raise commands.BadArgument(f"Set Limit first. `{ctx.prefix}claim limit`")

        cursor = self.db.find({'guild':str(self.bot.modmail_guild.id)})
        count = 0
        async for doc in cursor:
            if 'claimers' in doc and str(claimer_id) in doc['claimers']:
                if 'status' not in doc or doc['status'] != 'closed':
                    try:
                        channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                        if channel:
                            count += 1
                    except discord.NotFound:
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )

        return count < config['limit']

    async def check_before_update(self, channel):
        if channel.guild != self.bot.modmail_guild or await self.bot.api.get_log(channel.id) is None:
            return False

        return True

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """When a thread is deleted, mark it as closed in the database instead of deleting it"""
        if await self.check_before_update(channel):
            await self.db.find_one_and_update(
                {'thread_id': str(channel.id), 'guild': str(self.bot.modmail_guild.id)},
                {'$set': {'status': 'closed'}}
            )

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def unclaim(self, ctx):
        """Unclaim a thread"""
        if not await self.handle_thread_cooldown(ctx):
            return
            
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            try:
                await self.db.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
                    {'$pull': {'claimers': str(ctx.author.id)}}
                )
                
                recipient_id = match_user_id(ctx.thread.channel.topic)
                recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)
                await ctx.thread.channel.edit(name=recipient.name)
                
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
    @commands.group(name='claim', invoke_without_command=True)
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def claim_(self, ctx):
        """Claim a thread"""
        if not await self.handle_thread_cooldown(ctx):
            return

        if not ctx.invoked_subcommand:
            if not await self.check_claimer(ctx, ctx.author.id):
                await ctx.message.add_reaction('❌')
                return

            thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
            has_active_claimers = thread and thread.get('claimers') and len(thread['claimers']) > 0
            
            if not has_active_claimers:
                try:
                    # Subscribe
                    if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                        self.bot.config["subscriptions"][str(ctx.thread.id)] = []
                    if ctx.author.mention not in self.bot.config["subscriptions"][str(ctx.thread.id)]:
                        self.bot.config["subscriptions"][str(ctx.thread.id)].append(ctx.author.mention)
                        await self.bot.config.update()

                    # Update name and database
                    new_name = f"{ctx.author.display_name} claimed"
                    await ctx.thread.channel.edit(name=new_name)
                    
                    if thread is None:
                        await self.db.insert_one({
                            'thread_id': str(ctx.thread.channel.id), 
                            'guild': str(self.bot.modmail_guild.id), 
                            'claimers': [str(ctx.author.id)]
                        })
                    else:
                        await self.db.find_one_and_update(
                            {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)},
                            {
                                '$set': {'claimers': [str(ctx.author.id)]},
                                '$unset': {'status': ''}
                            }
                        )
                    
                    embed = discord.Embed(
                        color=self.bot.main_color,
                        description="Subscribed to thread.\nSuccessfully claimed the thread. Please respond to the case asap."
                    )
                    await ctx.send(embed=embed)
                except:
                    await ctx.message.add_reaction('❌')
            else:
                await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def change(self, ctx, *, member: discord.Member):
        """Change the claimer of the thread (Override permission required)"""
        if not await self.handle_thread_cooldown(ctx):
            return
            
        has_override = False
        if config := await self.db.find_one({'_id': 'config'}):
            if 'override_roles' in config:
                override_roles = [ctx.guild.get_role(r) for r in config['override_roles'] if ctx.guild.get_role(r) is not None]
                for role in override_roles:
                    if role in ctx.author.roles:
                        has_override = True
                        break

        if not has_override:
            await ctx.message.add_reaction('❌')
            return

        try:
            thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
            if thread:
                await self.db.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)},
                    {'$set': {'claimers': [str(member.id)]}}
                )
                
                new_name = f"{member.display_name} claimed"
                await ctx.thread.channel.edit(name=new_name)
                
                embed = discord.Embed(
                    color=self.bot.main_color,
                    description=f"Thread claimer changed to {member.mention}"
                )
                await ctx.send(embed=embed)
            else:
                await ctx.message.add_reaction('❌')
        except:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def rename(self, ctx, *, new_name: str):
        """Rename the current thread"""
        if not await self.handle_thread_cooldown(ctx):
            return
            
        try:
            await ctx.thread.channel.edit(name=new_name)
            await ctx.message.add_reaction('✅')
        except discord.HTTPException as e:
            if e.code == 429:
                await ctx.message.add_reaction('⏳')
                return
            await ctx.message.add_reaction('❌')
        except Exception:
            await ctx.message.add_reaction('❌')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
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
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def addclaim(self, ctx, *, member: discord.Member):
        """Adds another user to the thread claimers"""
        if not await self.check_claimer(ctx, member.id):
            return await ctx.reply(f"Limit reached, can't claim the thread.")

        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$addToSet': {'claimers': str(member.id)}})
            await ctx.send('Added to claimers')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def removeclaim(self, ctx, *, member: discord.Member):
        """Removes a user from the thread claimers"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$pull': {'claimers': str(member.id)}})
            await ctx.send('Removed from claimers')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def transferclaim(self, ctx, *, member: discord.Member):
        """Removes all users from claimers and gives another member all control over thread"""
        if not await self.check_claimer(ctx, member.id):
            return await ctx.reply(f"Limit reached, can't claim the thread.")

        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$set': {'claimers': [str(member.id)]}})
            await ctx.send('Added to claimers')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def overrideaddclaim(self, ctx, *, member: discord.Member):
        """Allow mods to bypass claim thread check in add"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$addToSet': {'claimers': str(member.id)}})
            await ctx.send('Added to claimers')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.command()
    async def claim_limit_(self, ctx, limit: int):
        """
        Set max threads a member can claim
        0 = No limit
        """
        if await self.db.find_one({'_id': 'config'}):
            await self.db.find_one_and_update({'_id': 'config'}, {'$set': {'limit': limit}})
        else:
            await self.db.insert_one({'_id': 'config', 'limit': limit})

        await ctx.send(f'Set limit to {limit}')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.group(name='bypass', invoke_without_command=True)
    async def claim_bypass_(self, ctx):
        """Manage bypass roles to claim check"""
        if not ctx.invoked_subcommand:
            if (roles_guild:= await self.db.find_one({'_id': 'config'})) and len(roles_guild['bypass_roles']) != 0:
                added = ", ".join(f"`{ctx.guild.get_role(r).name}`" for r in roles_guild['bypass_roles'])
                await ctx.send(f'By-pass roles: {added}')
            else:
                await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_bypass_.command(name='add')
    async def claim_bypass_add(self, ctx, *roles):
        """Add bypass roles to claim check"""
        bypass_roles = []
        for rol in roles:
            try:
                role = await commands.RoleConverter().convert(ctx, rol)
            except:
                role = discord.utils.find(
                    lambda r: r.name.lower() == rol.lower(), ctx.guild.roles
                )
            if role:
                bypass_roles.append(role)

        if len(bypass_roles) != 0:
            if await self.db.find_one({'_id': 'config'}):
                for role in bypass_roles:
                    await self.db.find_one_and_update({'_id': 'config'}, {'$addToSet': {'bypass_roles': role.id}})
            else:
                await self.db.insert_one({'_id': 'config', 'bypass_roles': [r.id for r in bypass_roles]})
            added = ", ".join(f"`{r.name}`" for r in bypass_roles)
           
        else:
            added = "`None`"

        await ctx.send(f'**Added to by-pass roles**:\n{added}')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_bypass_.command(name='remove')
    async def claim_bypass_remove(self, ctx, role: discord.Role):
        """Remove a bypass role from claim check"""
        roles_guild = await self.db.find_one({'_id': 'config'})
        if roles_guild and role.id in roles_guild['bypass_roles']:
            await self.db.find_one_and_update({'_id': 'config'}, {'$pull': {'bypass_roles': role.id}})
            await ctx.send(f'**Removed from by-pass roles**:\n`{role.name}`')
        else:
            await ctx.send(f'`{role.name}` is not in by-pass roles')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def overridereply(self, ctx, *, msg: str=""):
        """Allow mods to bypass claim thread check in reply"""
        await ctx.invoke(self.bot.get_command('reply'), msg=msg)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="stats")
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def claim_stats(self, ctx, member: discord.Member = None):
        """View comprehensive claim statistics for yourself or another member
        
        Shows both active and historical claims."""
        target = member or ctx.author
        
        # Get all claims for this user
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        active_claims = []
        closed_claims = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(target.id) in doc['claimers']:
                try:
                    channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                    if channel:
                        active_claims.append(doc)
                    else:
                        if 'status' not in doc or doc['status'] != 'closed':
                            await self.db.find_one_and_update(
                                {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                {'$set': {'status': 'closed'}}
                            )
                        closed_claims.append(doc)
                except (discord.NotFound, discord.Forbidden):
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
                    closed_claims.append(doc)
        
        total_claims = len(active_claims) + len(closed_claims)
        
        embed = discord.Embed(
            title=f"Claim Statistics for {target.display_name}",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        embed.add_field(
            name="Active Claims",
            value=str(len(active_claims)),
            inline=True
        )
        
        embed.add_field(
            name="Closed Claims",
            value=str(len(closed_claims)),
            inline=True
        )
        
        embed.add_field(
            name="Total Claims Ever",
            value=str(total_claims),
            inline=True
        )
        
        # Get claim limit
        config = await self.db.find_one({'_id': 'config'})
        limit = config.get('limit', 0) if config else 0
        limit_text = str(limit) if limit > 0 else "No limit"
        
        embed.add_field(
            name="Current Claim Limit", 
            value=limit_text,
            inline=True
        )
        
        embed.add_field(
            name="Claims Available", 
            value=str(limit - len(active_claims)) if limit > 0 else "∞",
            inline=True
        )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command(name="lb")
    @commands.cooldown(1, 60, commands.BucketType.channel)
    async def claim_leaderboard(self, ctx):
        """View the top 10 supporters by all-time claims including closed claims"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        active_claims = {}
        closed_claims = {}
        
        async for doc in cursor:
            if 'claimers' in doc:
                try:
                    channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                    for claimer_id in doc['claimers']:
                        if channel:
                            active_claims[claimer_id] = active_claims.get(claimer_id, 0) + 1
                            if 'status' in doc and doc['status'] == 'closed':
                                await self.db.find_one_and_update(
                                    {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                    {'$unset': {'status': ''}}
                                )
                        else:
                            closed_claims[claimer_id] = closed_claims.get(claimer_id, 0) + 1
                            if 'status' not in doc or doc['status'] != 'closed':
                                await self.db.find_one_and_update(
                                    {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                                    {'$set': {'status': 'closed'}}
                                )
                except (discord.NotFound, discord.Forbidden):
                    for claimer_id in doc['claimers']:
                        closed_claims[claimer_id] = closed_claims.get(claimer_id, 0) + 1
                    if 'status' not in doc or doc['status'] != 'closed':
                        await self.db.find_one_and_update(
                            {'thread_id': doc['thread_id'], 'guild': doc['guild']},
                            {'$set': {'status': 'closed'}}
                        )
        
        # Calculate total claims for each user
        all_claims = {}
        for user_id in set(list(active_claims.keys()) + list(closed_claims.keys())):
            all_claims[user_id] = active_claims.get(user_id, 0) + closed_claims.get(user_id, 0)
        
        sorted_claims = sorted(all_claims.items(), key=lambda x: x[1], reverse=True)[:10]
        
        embed = discord.Embed(
            title="Top Claimers - All Time Stats",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        for idx, (user_id, count) in enumerate(sorted_claims, 1):
            user = ctx.guild.get_member(int(user_id))
            name = user.display_name if user else f"Unknown User ({user_id})"
            
            active = active_claims.get(user_id, 0)
            closed = closed_claims.get(user_id, 0)
            embed.add_field(
                name=f"#{idx} {name}",
                value=f"Total: {count} claims\nActive: {active}\nClosed: {closed}",
                inline=False
            )
            
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.command(name="overview")
    async def claim_overview(self, ctx):
        """View detailed overview of the claim system and thread statistics"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        
        # Initialize counters
        stats = {
            'total_threads': 0,
            'active_threads': 0,
            'closed_threads': 0,
            'claimed_threads': 0,
            'unclaimed_threads': 0,
            'unique_claimers': set(),
            'most_active_claimers': {}
        }
        
        async for doc in cursor:
            stats['total_threads'] += 1
            
            # Check thread status
            if 'status' in doc and doc['status'] == 'closed':
                stats['closed_threads'] += 1
            else:
                stats['active_threads'] += 1
            
            # Check claim status
            if 'claimers' in doc and doc['claimers']:
                stats['claimed_threads'] += 1
                for claimer_id in doc['claimers']:
                    stats['unique_claimers'].add(claimer_id)
                    stats['most_active_claimers'][claimer_id] = stats['most_active_claimers'].get(claimer_id, 0) + 1
            else:
                stats['unclaimed_threads'] += 1
        
        # Create embed
        embed = discord.Embed(
            title="ModMail Claim System Overview",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        # Thread Statistics
        embed.add_field(
            name="Thread Statistics",
            value=f"Total Threads: {stats['total_threads']}\n"
                  f"Active Threads: {stats['active_threads']}\n"
                  f"Closed Threads: {stats['closed_threads']}\n"
                  f"Claimed Threads: {stats['claimed_threads']}\n"
                  f"Unclaimed Threads: {stats['unclaimed_threads']}",
            inline=False
        )
        
        # Calculate percentages
        if stats['total_threads'] > 0:
            claimed_percent = (stats['claimed_threads'] / stats['total_threads']) * 100
            closed_percent = (stats['closed_threads'] / stats['total_threads']) * 100
            embed.add_field(
                name="Percentages",
                value=f"Claimed: {claimed_percent:.1f}%\n"
                      f"Closed: {closed_percent:.1f}%",
                inline=True
            )
        
        # Unique claimers
        embed.add_field(
            name="Unique Claimers",
            value=str(len(stats['unique_claimers'])),
            inline=True
        )
        
        # Top 5 most active claimers
        if stats['most_active_claimers']:
            top_claimers = sorted(stats['most_active_claimers'].items(), key=lambda x: x[1], reverse=True)[:5]
            top_claimers_text = []
            for claimer_id, count in top_claimers:
                user = ctx.guild.get_member(int(claimer_id))
                name = user.display_name if user else f"Unknown User ({claimer_id})"
                top_claimers_text.append(f"{name}: {count} claims")
            
            embed.add_field(
                name="Top 5 Most Active Claimers",
                value="\n".join(top_claimers_text) if top_claimers_text else "No claims yet",
                inline=False
            )
        
        # Get current config
        config = await self.get_config()
        embed.add_field(
            name="Current Settings",
            value=f"Claim Limit: {config['limit'] if config['limit'] > 0 else 'No limit'}\n"
                  f"Command Cooldown: {config['command_cooldown']}s\n"
                  f"Thread Cooldown: {config['thread_cooldown']}s",
            inline=False
        )
        
        await ctx.send(embed=embed)

    async def get_cached_config(self):
        """Get cached config or fetch new if expired"""
        now = time.time()
        if now - self._cache_timestamp > 300:  # 5 minute cache
            self._config_cache = await self.get_config()
            self._cache_timestamp = now
        return self._config_cache

    async def get_config(self):
        """Get plugin configuration with defaults"""
        config = await self.db.find_one({'_id': 'config'}) or {}
        return {
            'limit': config.get('limit', self.default_config['limit']),
            'bypass_roles': config.get('bypass_roles', self.default_config['bypass_roles']),
            'override_roles': config.get('override_roles', self.default_config['override_roles']),
            'command_cooldown': config.get('command_cooldown', self.default_config['command_cooldown']),
            'thread_cooldown': config.get('thread_cooldown', self.default_config['thread_cooldown'])
        }

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.group(name='override', invoke_without_command=True)
    async def claim_override_(self, ctx):
        """Manage override roles that can reply to any thread regardless of claim status"""
        if not ctx.invoked_subcommand:
            config = await self.get_config()
            override_roles = config.get('override_roles', [])
            
            embed = discord.Embed(
                title="Override Roles Configuration",
                color=self.bot.main_color,
                timestamp=ctx.message.created_at
            )
            
            if override_roles:
                role_mentions = []
                invalid_roles = []
                for role_id in override_roles:
                    role = ctx.guild.get_role(role_id)
                    if role:
                        role_mentions.append(f"• {role.mention} (ID: {role.id})")
                    else:
                        invalid_roles.append(role_id)
                        
                # Clean up invalid roles
                if invalid_roles:
                    await self.db.find_one_and_update(
                        {'_id': 'config'},
                        {'$pull': {'override_roles': {'$in': invalid_roles}}}
                    )
                
                embed.description = "**Current Override Roles:**\n" + "\n".join(role_mentions)
                embed.add_field(
                    name="Usage",
                    value="• `override add <role>` - Add role to override list\n"
                          "• `override remove <role>` - Remove role from override list",
                    inline=False
                )
            else:
                embed.description = "No override roles configured."
                embed.add_field(
                    name="Setup",
                    value="Use `override add <role>` to add roles that can bypass claim restrictions.",
                    inline=False
                )
            
            await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_override_.command(name='add')
    async def claim_override_add(self, ctx, *roles):
        """Add roles that can reply to any thread regardless of claim status
        
        Usage: override add <role1> [role2] [role3] ...
        Accepts role mentions, IDs, or names
        """
        if not roles:
            embed = discord.Embed(
                title="Error",
                description="Please specify at least one role to add.",
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)
            
        override_roles = []
        invalid_roles = []
        
        for role_input in roles:
            try:
                # Try to convert the input to a role
                role = await commands.RoleConverter().convert(ctx, role_input)
                override_roles.append(role)
            except commands.RoleNotFound:
                invalid_roles.append(role_input)
        
        embed = discord.Embed(
            title="Add Override Roles",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        if override_roles:
            # Update database
            for role in override_roles:
                await self.db.find_one_and_update(
                    {'_id': 'config'},
                    {'$addToSet': {'override_roles': role.id}},
                    upsert=True
                )
            
            embed.add_field(
                name="Added Successfully",
                value="\n".join(f"• {role.mention}" for role in override_roles),
                inline=False
            )
        
        if invalid_roles:
            embed.add_field(
                name="Invalid Roles",
                value="\n".join(f"• {role}" for role in invalid_roles),
                inline=False
            )
            
        if not override_roles and not invalid_roles:
            embed.description = "No valid roles provided"
            embed.color = discord.Color.red()
            
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_override_.command(name='remove')
    async def claim_override_remove(self, ctx, *, role: discord.Role):
        """Remove a role from the override list
        
        Usage: override remove <role>
        Accepts role mention, ID, or name
        """
        config = await self.get_config()
        override_roles = config.get('override_roles', [])
        
        embed = discord.Embed(
            title="Remove Override Role",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        if role.id in override_roles:
            await self.db.find_one_and_update(
                {'_id': 'config'},
                {'$pull': {'override_roles': role.id}}
            )
            embed.description = f"Successfully removed {role.mention} from override roles"
            embed.color = discord.Color.green()
        else:
            embed.description = f"{role.mention} is not in the override roles list"
            embed.color = discord.Color.red()
        
        await ctx.send(embed=embed)

    async def remove_cooldown_reaction(self, ctx, seconds):
        """Remove cooldown reaction and message after specified time"""
        await asyncio.sleep(seconds)
        try:
            await ctx.message.delete()  # Delete the command message after cooldown
        except:
            pass

    # Generic error handler for all commands with cooldowns
    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            try:
                await ctx.message.add_reaction('⏳')
                # Start task to remove message after cooldown
                asyncio.create_task(self.remove_cooldown_reaction(ctx, int(error.retry_after)))
            except:
                pass

    async def thread_command_cooldown(self, ctx):
        """Check if thread is on cooldown for any command"""
        if not ctx.thread:
            return True
            
        config = await self.get_config()
        current_time = time.time()
        thread_id = str(ctx.thread.channel.id)
        
        if thread_id in self.thread_cooldowns:
            if current_time - self.thread_cooldowns[thread_id] < config['thread_cooldown']:
                return False
        
        self.thread_cooldowns[thread_id] = current_time
        return True

    async def handle_thread_cooldown(self, ctx):
        """Handle cooldown for thread commands"""
        if not await self.thread_command_cooldown(ctx):
            await ctx.message.add_reaction('⏳')
            asyncio.create_task(self.remove_cooldown_reaction(ctx, config['thread_cooldown']))
            return False
        return True

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.group(name='claimconfig', invoke_without_command=True)
    async def claim_config(self, ctx):
        """Configure claim plugin settings"""
        if ctx.invoked_subcommand is None:
            config = await self.get_config()
            embed = discord.Embed(
                title="Claim Plugin Configuration",
                color=self.bot.main_color
            )
            embed.add_field(name="Command Cooldown", value=f"{config['command_cooldown']} seconds", inline=True)
            embed.add_field(name="Thread Cooldown", value=f"{config['thread_cooldown']} seconds", inline=True)
            embed.add_field(name="Claim Limit", value=str(config['limit']), inline=True)
            await ctx.send(embed=embed)

    @claim_config.command(name='cooldown')
    async def set_command_cooldown(self, ctx, seconds: int):
        """Set the cooldown for claim commands"""
        if seconds < 0:
            return await ctx.send("Cooldown cannot be negative")
            
        await self.db.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'command_cooldown': seconds}},
            upsert=True
        )
        await ctx.send(f"Command cooldown set to {seconds} seconds")

    @claim_config.command(name='threadcooldown')
    async def set_thread_cooldown(self, ctx, seconds: int):
        """Set the cooldown for thread operations"""
        if seconds < 0:
            return await ctx.send("Cooldown cannot be negative")
            
        await self.db.find_one_and_update(
            {'_id': 'config'},
            {'$set': {'thread_cooldown': seconds}},
            upsert=True
        )
        await ctx.send(f"Thread cooldown set to {seconds} seconds")


async def check_reply(ctx):
    thread = await ctx.bot.get_cog('ClaimThread').db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(ctx.bot.modmail_guild.id)})
    if thread and len(thread['claimers']) != 0:
        in_role = False
        has_override = False
        if config := await ctx.bot.get_cog('ClaimThread').db.find_one({'_id': 'config'}):
            # Check bypass roles
            if 'bypass_roles' in config:
                roles = [ctx.guild.get_role(r) for r in config['bypass_roles'] if ctx.guild.get_role(r) is not None]
                for role in roles:
                    if role in ctx.author.roles:
                        in_role = True
            # Check override roles
            if 'override_roles' in config:
                override_roles = [ctx.guild.get_role(r) for r in config['override_roles'] if ctx.guild.get_role(r) is not None]
                for role in override_roles:
                    if role in ctx.author.roles:
                        has_override = True
        return ctx.author.bot or in_role or has_override or str(ctx.author.id) in thread['claimers']
    return True


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
