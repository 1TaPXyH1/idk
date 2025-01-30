import discord
from discord.ext import commands
from discord.ui import Button, View

from core import checks
from core.checks import PermissionLevel


class ClaimButtonView(View):
    def __init__(self, db, thread_id):
        super().__init__(timeout=None)
        self.db = db
        self.thread_id = str(thread_id)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.blurple)
    async def claim_button(self, interaction: discord.Interaction, button: Button):
        thread = await self.db.find_one({'thread_id': self.thread_id})
        if thread is None:
            # Claim the ticket and disable the button
            await self.db.insert_one({'thread_id': self.thread_id, 'claimer': str(interaction.user.id)})
            button.label = f"Claimed by {interaction.user.name}"
            button.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("You have claimed this ticket.", ephemeral=True)
        else:
            # Notify the user that the ticket is already claimed
            claimer_id = thread['claimer']
            claimer = interaction.guild.get_member(int(claimer_id))
            claimer_name = claimer.name if claimer else "Unknown User"
            await interaction.response.send_message(
                f"This ticket has already been claimed by {claimer_name}.", ephemeral=True
            )


class ClaimTicket(commands.Cog):
    """Automatically send a 'Claim Ticket' button when a thread is created."""

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        """Send a message with a 'Claim Ticket' button when a thread is created."""
        embed = discord.Embed(
            title="New Ticket",
            description="Click the button below to claim this ticket.",
            color=discord.Color.blurple()
        )
        view = ClaimButtonView(self.db, thread.id)
        await thread.send(embed=embed, view=view)  # Removed `content=None`

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command()
    async def checkclaimer(self, ctx):
        """Check who claimed the current thread."""
        thread = await self.db.find_one({'thread_id': str(ctx.channel.id)})
        if thread and 'claimer' in thread:
            claimer_id = thread['claimer']
            claimer = ctx.guild.get_member(int(claimer_id))
            claimer_name = claimer.name if claimer else "Unknown User"
            await ctx.send(f"This ticket was claimed by {claimer_name}.")
        else:
            await ctx.send("This ticket has not been claimed yet.")

    @checks.has_permissions(PermissionLevel.MODERATOR)
    @commands.command()
    async def clearclaim(self, ctx):
        """Clear the claim on the current thread."""
        thread = await self.db.find_one({'thread_id': str(ctx.channel.id)})
        if thread:
            await self.db.delete_one({'thread_id': str(ctx.channel.id)})
            await ctx.send("The claim has been cleared for this thread.")
        else:
            await ctx.send("No claim exists for this thread.")


async def setup(bot):
    await bot.add_cog(ClaimTicket(bot))
