# Credits and orignal author: https://github.com/fourjr/modmail-plugins/blob/master/claim/claim.py
# Slightly modified for Minion_Kadin#2022 (discord)
# Please use the original plugin as this one may cause your bot to nuke the world

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id


class ClaimThread(commands.Cog):
    """Allows supporters to claim thread by sending claim in the thread channel"""
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        check_reply.fail_msg = 'This thread has been claimed by another user.'
        self.bot.get_command('reply').add_check(check_reply)
        self.bot.get_command('areply').add_check(check_reply)
        self.bot.get_command('fareply').add_check(check_reply)
        self.bot.get_command('freply').add_check(check_reply)

    async def check_claimer(self, ctx, claimer_id):
        config = await self.db.find_one({'_id': 'config'})
        if config and 'limit' in config:
            if config['limit'] == 0:
                return True
        else:
            raise commands.BadArgument(f"Set Limit first. `{ctx.prefix}claim limit`")

        cursor = self.db.find({'guild':str(self.bot.modmail_guild.id)})
        count = 0
        async for x in cursor:
            if 'claimers' in x and str(claimer_id) in x['claimers']:
                count += 1

        return count < config['limit']

    async def check_before_update(self, channel):
        if channel.guild != self.bot.modmail_guild or await self.bot.api.get_log(channel.id) is None:
            return False

        return True

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if await self.check_before_update(channel):
            await self.db.delete_one({'thread_id': str(channel.id), 'guild': str(self.bot.modmail_guild.id)})

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.group(name='claim', invoke_without_command=True)
    async def claim_(self, ctx, subscribe: bool = True):
        """Claim a thread"""
        if not ctx.invoked_subcommand:
            if not await self.check_claimer(ctx, ctx.author.id):
                return await ctx.reply(f"Limit reached, can't claim the thread.")

            thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
            recipient_id = match_user_id(ctx.thread.channel.topic)
            recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)

            description = ""
            if subscribe:
                if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
                    self.bot.config["subscriptions"][str(ctx.thread.id)] = []

                mentions = self.bot.config["subscriptions"][str(ctx.thread.id)]

                if ctx.author.mention in mentions:
                    mentions.remove(ctx.author.mention)
                    description += f"{ctx.author.mention} will __not__ be notified of any message now.\n"
                else:
                    mentions.append(ctx.author.mention)
                    description += f"{ctx.author.mention} will now be notified of all messages received.\n"
                await self.bot.config.update()

            embed = discord.Embed(color=self.bot.main_color)
            if thread is None or (thread and len(thread['claimers']) == 0):
                new_name = f"{ctx.author.display_name} claimed"
                try:
                    await ctx.thread.channel.edit(name=new_name)
                except discord.Forbidden:
                    description += "\nFailed to rename channel (Missing Permissions)"
                except discord.HTTPException:
                    description += "\nFailed to rename channel"

                if thread is None:
                    await self.db.insert_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id), 'claimers': [str(ctx.author.id)]})
                else:
                    await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$addToSet': {'claimers': str(ctx.author.id)}})
                
                description += "Please respond to the case asap."
                embed.description = description
                await ctx.reply(embed=embed)
            else:
                description += "Thread is already claimed"
                embed.description = description
                await ctx.reply(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def claims(self, ctx):
        """Check which channels you have clamined"""
        cursor = self.db.find({'guild':str(self.bot.modmail_guild.id)})
        channels = []
        async for x in cursor:
            if 'claimers' in x and str(ctx.author.id) in x['claimers']:
                try:
                    channel = ctx.guild.get_channel(int(x['thread_id'])) or await self.bot.fetch_channel(int(x['thread_id']))
                except discord.NotFound:
                    channel = None
                    await self.db.delete_one({'thread_id': x['thread_id'], 'guild': x['guild']})

                if channel and channel not in channels:
                    channels.append(channel)

        embed = discord.Embed(title='Your claimed tickets:', color=self.bot.main_color)
        embed.description = ', '.join(ch.mention for ch in channels)
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @claim_.command()
    async def cleanup(self, ctx):
        """Cleans up the database for deleted tickets"""
        cursor = self.db.find({'guild':str(self.bot.modmail_guild.id)})
        count = 0
        async for x in cursor:
            try:
                channel = ctx.guild.get_channel(int(x['thread_id'])) or await self.bot.fetch_channel(int(x['thread_id']))
            except discord.NotFound:
                await self.db.delete_one({'thread_id': x['thread_id'], 'guild': x['guild']})
                count += 1

        embed = discord.Embed(color=self.bot.main_color)
        embed.description = f"Cleaned up {count} closed tickets records"
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
    async def unclaim(self, ctx):
        """Unclaim a thread"""
        embed = discord.Embed(color=self.bot.main_color)
        description = ""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$pull': {'claimers': str(ctx.author.id)}})
            description += 'Removed from claimers.\n'
            
            recipient_id = match_user_id(ctx.thread.channel.topic)
            recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)
            try:
                await ctx.thread.channel.edit(name=recipient.name)
            except discord.Forbidden:
                description += "\nFailed to rename channel (Missing Permissions)"
            except discord.HTTPException:
                description += "\nFailed to rename channel"

        if str(ctx.thread.id) not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][str(ctx.thread.id)] = []

        mentions = self.bot.config["subscriptions"][str(ctx.thread.id)]

        if ctx.author.mention in mentions:
            mentions.remove(ctx.author.mention)
            await self.bot.config.update()
            description += f"{ctx.author.mention} is now unsubscribed from this thread."

        if description == "":
            description = "Nothing to do"

        embed.description = description
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    @commands.command()
    async def forceclaim(self, ctx, *, member: discord.Member):
        """Make a user froce claim an already claimed thread"""
        if not await self.check_claimer(ctx, member.id):
            return await ctx.reply(f"Limit reached, can't claim the thread.")

        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread is None:
            await self.db.insert_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id), 'claimers': [str(member.id)]})
            await ctx.send(f'{member.name} is added to claimers')
        elif str(member.id) not in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$addToSet': {'claimers': str(member.id)}})
            await ctx.send(f'{member.name} is added to claimers')
        else:
            await ctx.send(f'{member.name} is already in claimers')

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    @commands.command()
    async def forceunclaim(self, ctx, *, member: discord.Member):
        """Force remove a user from the thread claimers"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread:
            if str(member.id) in thread['claimers']:
                await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$pull': {'claimers': str(member.id)}})
                await ctx.send(f'{member.name} is removed from claimers')
            else:
                await ctx.send(f'{member.name} is not in claimers')
        else:
            await ctx.send(f'No one claimed this thread yet')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
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
    async def removeclaim(self, ctx, *, member: discord.Member):
        """Removes a user from the thread claimers"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread and str(ctx.author.id) in thread['claimers']:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$pull': {'claimers': str(member.id)}})
            await ctx.send('Removed from claimers')

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.command()
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
    async def overrideaddclaim(self, ctx, *, member: discord.Member):
        """Allow mods to bypass claim thread check in add"""
        thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
        if thread:
            await self.db.find_one_and_update({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, {'$addToSet': {'claimers': str(member.id)}})
            await ctx.send('Added to claimers')


    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.guild_only()
    @claim_.command(name='limit')
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
    @claim_.group(name='bypass', invoke_without_command=True)
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
    async def overridereply(self, ctx, *, msg: str=""):
        """Allow mods to bypass claim thread check in reply"""
        await ctx.invoke(self.bot.get_command('reply'), msg=msg)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @claim_.command(name="stats")
    async def claim_stats(self, ctx, member: discord.Member = None):
        """View comprehensive claim statistics for yourself or another member
        
        Shows both active and historical claims."""
        target = member or ctx.author
        
        # Get all claims for this user
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        current_claims = 0
        total_claims = 0
        closed_claims = 0
        
        async for doc in cursor:
            if 'claimers' in doc and str(target.id) in doc['claimers']:
                total_claims += 1
                # Check if channel still exists (active claim)
                try:
                    channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                    if channel:
                        current_claims += 1
                    else:
                        closed_claims += 1
                except (discord.NotFound, discord.Forbidden):
                    closed_claims += 1
        
        embed = discord.Embed(
            title=f"Claim Statistics for {target.display_name}",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        # Current claims
        embed.add_field(
            name="Active Claims",
            value=str(current_claims),
            inline=True
        )
        
        # Historical claims
        embed.add_field(
            name="Closed Claims",
            value=str(closed_claims),
            inline=True
        )
        
        # Total claims ever
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
            value=str(limit - current_claims) if limit > 0 else "∞",
            inline=True
        )
        
        # Calculate percentage of total server claims
        total_server_claims = 0
        async for _ in self.db.find({'guild': str(self.bot.modmail_guild.id)}):
            total_server_claims += 1
            
        if total_server_claims > 0:
            percentage = (total_claims / total_server_claims) * 100
            embed.add_field(
                name="Percentage of Total Server Claims",
                value=f"{percentage:.1f}%",
                inline=True
            )
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @claim_.command(name="leaderboard", aliases=["lb"])
    async def claim_leaderboard(self, ctx, show_all: bool = False):
        """View the top 10 supporters by claims
        
        Use '?claim leaderboard true' to see all-time stats including closed claims
        Use '?claim leaderboard' to see only active claims"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        active_claims = {}
        all_claims = {}
        
        async for doc in cursor:
            if 'claimers' in doc:
                # Track all claims
                for claimer_id in doc['claimers']:
                    all_claims[claimer_id] = all_claims.get(claimer_id, 0) + 1
                    
                    # Check if claim is still active
                    try:
                        channel = ctx.guild.get_channel(int(doc['thread_id'])) or await self.bot.fetch_channel(int(doc['thread_id']))
                        if channel:
                            active_claims[claimer_id] = active_claims.get(claimer_id, 0) + 1
                    except (discord.NotFound, discord.Forbidden):
                        continue
        
        # Use appropriate data based on show_all parameter
        claims_data = all_claims if show_all else active_claims
        sorted_claims = sorted(claims_data.items(), key=lambda x: x[1], reverse=True)[:10]
        
        embed = discord.Embed(
            title=f"Top Claimers - {'All Time' if show_all else 'Active Claims'}",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        for idx, (user_id, count) in enumerate(sorted_claims, 1):
            user = ctx.guild.get_member(int(user_id))
            name = user.display_name if user else f"Unknown User ({user_id})"
            
            if show_all:
                active_count = active_claims.get(user_id, 0)
                closed_count = count - active_count
                embed.add_field(
                    name=f"#{idx} {name}",
                    value=f"Total: {count} claims\nActive: {active_count}\nClosed: {closed_count}",
                    inline=False
                )
            else:
                embed.add_field(
                    name=f"#{idx} {name}",
                    value=f"{count} active claims",
                    inline=False
                )
            
        embed.set_footer(text="Use '?claim leaderboard true' to see all-time stats")
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @claim_.command(name="overview")
    async def claim_overview(self, ctx):
        """View overall claim statistics"""
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        
        total_threads = 0
        claimed_threads = 0
        unique_claimers = set()
        
        async for doc in cursor:
            total_threads += 1
            if 'claimers' in doc and doc['claimers']:
                claimed_threads += 1
                unique_claimers.update(doc['claimers'])
        
        embed = discord.Embed(
            title="Claim System Overview",
            color=self.bot.main_color
        )
        
        embed.add_field(name="Total Active Threads", value=str(total_threads))
        embed.add_field(name="Claimed Threads", value=str(claimed_threads))
        embed.add_field(name="Unclaimed Threads", value=str(total_threads - claimed_threads))
        embed.add_field(name="Active Claimers", value=str(len(unique_claimers)))
        
        # Calculate percentage of claimed threads
        if total_threads > 0:
            percentage = (claimed_threads / total_threads) * 100
            embed.add_field(name="Claim Percentage", value=f"{percentage:.1f}%")
        
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @commands.command()
    async def claimhistory(self, ctx, member: discord.Member = None):
        """View claim history for yourself or another member
        
        Usage: ?claimhistory [member]
        Example: ?claimhistory @ModeratorBob"""
        target = member or ctx.author
        
        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        claims = []
        
        async for doc in cursor:
            if 'claimers' in doc and str(target.id) in doc['claimers']:
                channel = ctx.guild.get_channel(int(doc['thread_id']))
                if channel:
                    claims.append(channel)
        
        embed = discord.Embed(
            title=f"Claim History for {target.display_name}",
            color=self.bot.main_color,
            timestamp=ctx.message.created_at
        )
        
        if claims:
            claim_list = "\n".join([f"• {channel.name} ({channel.mention})" for channel in claims[-10:]])
            embed.description = f"**Last 10 Claims:**\n{claim_list}"
        else:
            embed.description = "No active claims found."
            
        embed.set_footer(text=f"Total Active Claims: {len(claims)}")
        await ctx.send(embed=embed)

async def check_reply(ctx):
    thread = await ctx.bot.get_cog('ClaimThread').db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(ctx.bot.modmail_guild.id)})
    if thread and len(thread['claimers']) != 0:
        in_role = False
        if config:= await ctx.bot.get_cog('ClaimThread').db.find_one({'_id': 'config'}):
            if 'bypass_roles' in config:
                roles = [ctx.guild.get_role(r) for r in config['bypass_roles'] if ctx.guild.get_role(r) is not None]
                for role in roles:
                    if role in ctx.author.roles:
                        in_role = True
        return ctx.author.bot or in_role or str(ctx.author.id) in thread['claimers']
    return True


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
