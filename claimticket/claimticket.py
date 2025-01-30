import discord
from discord.ext import commands
from discord import ui
from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id

class ClaimThread(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        check_reply.fail_msg = 'This thread has been claimed by another user.'
        self.bot.get_command('reply').add_check(check_reply)
        self.bot.get_command('areply').add_check(check_reply)
        self.bot.get_command('fareply').add_check(check_reply)
        self.bot.get_command('freply').add_check(check_reply)

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

    async def check_claimer(self, ctx, claimer_id):
        config = await self.db.find_one({'_id': 'config'})
        if config and 'limit' in config:
            if config['limit'] == 0:
                return True
        else:
            raise commands.BadArgument(f"Set Limit first. `{ctx.prefix}claim limit`")

        cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
        count = 0
        async for x in cursor:
            if 'claimers' in x and str(claimer_id) in x['claimers']:
                count += 1

        return count < config['limit']

    async def check_before_update(self, thread):
        """Check if the thread is a valid modmail thread"""
        # For threads, we need to get the parent channel's guild
        if not isinstance(thread, discord.Thread):
            return False
            
        parent_channel = thread.parent
        if not parent_channel:
            return False
            
        if parent_channel.guild != self.bot.modmail_guild:
            return False
            
        # Check if this is a modmail thread
        if await self.bot.api.get_log(thread.id) is None:
            return False
            
        return True

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

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if await self.check_before_update(channel):
            await self.db.delete_one({'thread_id': str(channel.id), 'guild': str(self.bot.modmail_guild.id)})

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
