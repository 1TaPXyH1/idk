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
        check_reply.fail_msg = 'This thread has been claimed by another user.'
        self.bot.get_command('reply').add_check(check_reply)
        self.bot.get_command('areply').add_check(check_reply)
        self.bot.get_command('fareply').add_check(check_reply)
        self.bot.get_command('freply').add_check(check_reply)

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
                
                await ctx.message.add_reaction('✅')
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
        try:
            channel = self.bot.get_channel(ctx.channel.id)
            if not channel:
                return
        except:
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
                    
                    await ctx.message.add_reaction('✅')
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
                await ctx.message.add_reaction('✅')
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
    @commands.command()
    async def claim_overview(self, ctx):
        """View overall claim statistics"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        
        total_threads = 0
        active_threads = 0
        closed_threads = 0
        
        async for doc in cursor:
            total_threads += 1
            if 'status' in doc and doc['status'] == 'closed':
                closed_threads += 1
            else:
                active_threads += 1
        
        embed = discord.Embed(
            title="Claim System Overview",
            color=self.bot.main_color
        )
        
        embed.add_field(name="Total Claims", value=str(total_threads), inline=True)
        embed.add_field(name="Active Claims", value=str(active_threads), inline=True)
        embed.add_field(name="Closed Claims", value=str(closed_threads), inline=True)
        
        if total_threads > 0:
            closure_percentage = (closed_threads / total_threads) * 100
            embed.add_field(name="Closure Percentage", value=f"{closure_percentage:.1f}%", inline=True)
        
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
            'limit': config.get('limit', 0),
            'bypass_roles': config.get('bypass_roles', []),
            'override_roles': config.get('override_roles', [])
        }

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @commands.group(name='override', invoke_without_command=True)
    async def claim_override_(self, ctx):
        """Manage override roles that can reply to any thread regardless of claim status"""
        if not ctx.invoked_subcommand:
            embed = discord.Embed(
                title="Override Roles",
                color=self.bot.main_color
            )
            if (roles_guild := await self.db.find_one({'_id': 'config'})) and roles_guild.get('override_roles', []):
                roles_text = "\n".join(f"• {ctx.guild.get_role(r).mention}" for r in roles_guild['override_roles'] if ctx.guild.get_role(r))
                embed.description = roles_text
            else:
                embed.description = "No override roles configured. Use `claim override add <role>` to add roles."
            await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_override_.command(name='add')
    async def claim_override_add(self, ctx, *roles):
        """Add roles that can reply to any thread regardless of claim status"""
        override_roles = []
        for rol in roles:
            try:
                role = await commands.RoleConverter().convert(ctx, rol)
            except:
                role = discord.utils.find(
                    lambda r: r.name.lower() == rol.lower(), ctx.guild.roles
                )
            if role:
                override_roles.append(role)

        embed = discord.Embed(
            title="Add Override Roles",
            color=self.bot.main_color
        )

        if len(override_roles) != 0:
            if await self.db.find_one({'_id': 'config'}):
                for role in override_roles:
                    await self.db.find_one_and_update(
                        {'_id': 'config'}, 
                        {'$addToSet': {'override_roles': role.id}}
                    )
            else:
                await self.db.insert_one({
                    '_id': 'config', 
                    'override_roles': [r.id for r in override_roles]
                })
            added = "\n".join(f"• {r.mention}" for r in override_roles)
            embed.description = f"**Added to override roles**:\n{added}"
        else:
            embed.description = "No valid roles provided"
            embed.color = discord.Color.red()

        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.channel)
    @claim_override_.command(name='remove')
    async def claim_override_remove(self, ctx, role: discord.Role):
        """Remove a role from the override list"""
        embed = discord.Embed(
            title="Remove Override Role",
            color=self.bot.main_color
        )

        roles_guild = await self.db.find_one({'_id': 'config'})
        if roles_guild and role.id in roles_guild['override_roles']:
            await self.db.find_one_and_update(
                {'_id': 'config'}, 
                {'$pull': {'override_roles': role.id}}
            )
            embed.description = f"**Removed from override roles**:\n• {role.mention}"
        else:
            embed.description = f"{role.mention} is not in override roles"
            embed.color = discord.Color.red()

        await ctx.send(embed=embed)

    async def remove_cooldown_reaction(self, ctx, seconds):
        """Remove cooldown reaction after specified time"""
        await asyncio.sleep(seconds)
        try:
            await ctx.message.remove_reaction('⏳', self.bot.user)
            await ctx.message.add_reaction('🔄')  # Shows cooldown is over
            await asyncio.sleep(5)  # Show the 🔄 for 5 seconds
            await ctx.message.remove_reaction('🔄', self.bot.user)
        except:
            pass

    # Generic error handler for all commands with cooldowns
    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            try:
                await ctx.message.add_reaction('⏳')
                # Start task to remove cooldown reaction
                asyncio.create_task(self.remove_cooldown_reaction(ctx, int(error.retry_after)))
            except:
                pass


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
