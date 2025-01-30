# Credits and orignal author: https://github.com/fourjr/modmail-plugins/blob/master/claim/claim.py
# Slightly modified for Minion_Kadin#2022 (discord)
# Please use the original plugin as this one may cause your bot to nuke the world

import discord
from discord.ext import commands
import traceback
from datetime import datetime

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
        self.log_channel_id = None

    # Button class for the claim functionality
    class ClaimButton(ui.Button):
        def __init__(self):
            super().__init__(
                label="Claim Ticket",
                style=discord.ButtonStyle.primary,
                custom_id="claim_ticket"
            )

        async def callback(self, interaction: discord.Interaction):
            # Get the cog instance
            cog = interaction.client.get_cog("ClaimThread")
            
            # Check if user has supporter permissions
            if not await commands.has_permissions(PermissionLevel.SUPPORTER).predicate(interaction):
                return await interaction.response.send_message(
                    "You don't have permission to claim tickets.", 
                    ephemeral=True
                )
            
            # Check claim limit
            if not await cog.check_claimer(interaction, interaction.user.id):
                return await interaction.response.send_message(
                    "Limit reached, can't claim the thread.", 
                    ephemeral=True
                )

            # Get thread info from database
            thread = await cog.db.find_one({
                'thread_id': str(interaction.channel.id),
                'guild': str(interaction.guild.id)
            })

            # Check if thread is already claimed
            if thread and thread.get('claimers', []):
                return await interaction.response.send_message(
                    "This ticket is already claimed.", 
                    ephemeral=True
                )

            # Claim the thread
            if not thread:
                await cog.db.insert_one({
                    'thread_id': str(interaction.channel.id),
                    'guild': str(interaction.guild.id),
                    'claimers': [str(interaction.user.id)]
                })
            else:
                await cog.db.find_one_and_update(
                    {
                        'thread_id': str(interaction.channel.id), 
                        'guild': str(interaction.guild.id)
                    },
                    {'$addToSet': {'claimers': str(interaction.user.id)}}
                )

            # Update button state
            self.disabled = True
            self.label = f"Claimed by {interaction.user.display_name}"
            self.style = discord.ButtonStyle.secondary
            
            # Update the message with the new button state
            await interaction.message.edit(view=self.view)

            # Send confirmation message
            await interaction.response.send_message(
                f"Ticket claimed by {interaction.user.mention}", 
                ephemeral=True
            )

            # Send notification to recipient
            try:
                recipient_id = match_user_id(interaction.channel.topic)
                if recipient_id:
                    recipient = interaction.client.get_user(recipient_id) or await interaction.client.fetch_user(recipient_id)
                    embed = discord.Embed(
                        color=interaction.client.main_color,
                        title="Ticket Claimed",
                        description="Please wait as the assigned support agent reviews your case, you will receive a response shortly.",
                        timestamp=discord.utils.utcnow(),
                    )
                    embed.set_footer(
                        text=f"{interaction.user.name}#{interaction.user.discriminator}", 
                        icon_url=interaction.user.display_avatar.url
                    )
                    await recipient.send(embed=embed)
            except:
                pass

    # View class to hold the button
    class ClaimView(ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.add_item(ClaimThread.ClaimButton())

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        """Send claim button when a new thread is created"""
        if not await self.check_before_update(thread):
            return

        embed = discord.Embed(
            title="Ticket Controls",
            description="Click the button below to claim this ticket.",
            color=self.bot.main_color
        )
        
        await thread.send(embed=embed, view=self.ClaimView())

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

    async def log_action(self, ctx, action: str, target=None, error=None):
        """Log claim actions to designated channel"""
        try:
            if not self.log_channel_id:
                return
                
            log_channel = self.bot.get_channel(self.log_channel_id)
            if not log_channel:
                return

            embed = discord.Embed(
                title="Claim Action Log",
                color=discord.Color.blue() if not error else discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            
            embed.add_field(
                name="Action", 
                value=action, 
                inline=False
            )
            embed.add_field(
                name="Thread", 
                value=ctx.channel.mention, 
                inline=True
            )
            embed.add_field(
                name="User", 
                value=ctx.author.mention, 
                inline=True
            )
            
            if target:
                embed.add_field(
                    name="Target", 
                    value=target.mention, 
                    inline=True
                )
            
            if error:
                embed.add_field(
                    name="Error", 
                    value=f"```{str(error)}```", 
                    inline=False
                )
                
            embed.set_footer(text=f"Thread ID: {ctx.channel.id}")
            await log_channel.send(embed=embed)
            
        except Exception as e:
            print(f"Logging error: {str(e)}")

    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.group(name='claim', invoke_without_command=True)
    async def claim_(self, ctx, subscribe: bool = True):
        """Claim a thread"""
        try:
            if not ctx.invoked_subcommand:
                if not await self.check_claimer(ctx, ctx.author.id):
                    await self.log_action(ctx, "Claim Attempt Failed", error="Limit reached")
                    return await ctx.reply(f"Limit reached, can't claim the thread.")

                thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
                recipient_id = match_user_id(ctx.thread.channel.topic)
                recipient = self.bot.get_user(recipient_id) or await self.bot.fetch_user(recipient_id)

                # Create the claim embed
                embed = discord.Embed(
                    color=self.bot.main_color,
                    title="Ticket Claimed",
                    description="Please wait as the assigned support agent reviews your case, you will receive a response shortly.",
                    timestamp=ctx.message.created_at,
                )
                embed.set_footer(
                    text=f"{ctx.author.name}#{ctx.author.discriminator}", 
                    icon_url=ctx.author.display_avatar.url
                )

                description = ""
                
                # Handle subscription
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

                # Handle claiming
                if thread is None or len(thread.get('claimers', [])) == 0:
                    # Update database
                    if thread is None:
                        await self.db.insert_one({
                            'thread_id': str(ctx.thread.channel.id), 
                            'guild': str(self.bot.modmail_guild.id), 
                            'claimers': [str(ctx.author.id)]
                        })
                    else:
                        await self.db.find_one_and_update(
                            {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
                            {'$addToSet': {'claimers': str(ctx.author.id)}}
                        )
                    
                    # Rename channel
                    try:
                        new_name = f"{ctx.channel.name}-claimed-by-{ctx.author.name}"
                        await ctx.channel.edit(name=new_name)
                    except discord.Forbidden:
                        description += "\nCouldn't rename channel - missing permissions."
                    except Exception as e:
                        description += f"\nError renaming channel: {str(e)}"
                    
                    # Send notifications
                    async with ctx.typing():
                        await recipient.send(embed=embed)
                    description += "Please respond to the case asap."
                    embed.description = description
                    await ctx.reply(embed=embed)
                    
                    # Log the successful claim
                    await self.log_action(ctx, "Ticket Claimed")
                    
                else:
                    description += "Thread is already claimed"
                    embed.description = description
                    await ctx.reply(embed=embed)
                    await self.log_action(ctx, "Claim Attempt Failed", error="Already claimed")
                    
        except Exception as e:
            error_traceback = traceback.format_exc()
            await self.log_action(ctx, "Claim Error", error=error_traceback)
            await ctx.send(f"An error occurred while claiming the thread: {str(e)}")

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
        try:
            embed = discord.Embed(color=self.bot.main_color)
            description = ""
            thread = await self.db.find_one({'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)})
            
            if thread and str(ctx.author.id) in thread['claimers']:
                await self.db.find_one_and_update(
                    {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
                    {'$pull': {'claimers': str(ctx.author.id)}}
                )
                description += 'Removed from claimers.\n'
                
                # Rename channel back
                try:
                    original_name = ctx.channel.name.split('-claimed-by-')[0]
                    await ctx.channel.edit(name=original_name)
                except discord.Forbidden:
                    description += "\nCouldn't rename channel - missing permissions."
                except Exception as e:
                    description += f"\nError renaming channel: {str(e)}"
                
                await self.log_action(ctx, "Ticket Unclaimed")

            # Rest of the unclaim code...
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
            
        except Exception as e:
            error_traceback = traceback.format_exc()
            await self.log_action(ctx, "Unclaim Error", error=error_traceback)
            await ctx.send(f"An error occurred while unclaiming the thread: {str(e)}")

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
