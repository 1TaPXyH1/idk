import discord
from discord.ext import commands
from discord.ui import Button, View

from core import checks
from core.checks import PermissionLevel

class ClaimThread(commands.Cog):
    """Allows supporters to claim threads using a button."""
    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        """Send a message with a 'Claim Ticket' button when a thread is created."""
        embed = discord.Embed(
            title="New Ticket",
            description="Click the button below to claim this ticket.",
            color=discord.Color.blurple()
        )
        view = ClaimButtonView(self.db, thread.id)
        await thread.send(embed=embed, view=view)


class ClaimButtonView(View):
    def __init__(self, db, thread_id):
        super().__init__(timeout=None)
        self.db = db
        self.thread_id = thread_id

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.blurple)
    async def claim_ticket(self, interaction: discord.Interaction, button: Button):
        """Handles the claiming of a ticket."""
        # Check if the thread is already claimed
        thread = await self.db.find_one({"thread_id": str(self.thread_id)})

        if thread:
            claimer_id = thread.get("claimer")
            if str(interaction.user.id) == claimer_id:
                await interaction.response.send_message(
                    "You have already claimed this ticket!", ephemeral=True
                )
                return

            await interaction.response.send_message(
                f"This ticket has already been claimed by <@{claimer_id}>.", ephemeral=True
            )
        else:
            # Claim the thread
            await self.db.insert_one({"thread_id": str(self.thread_id), "claimer": str(interaction.user.id)})

            # Disable the button and update the message
            button.disabled = True
            button.label = f"Claimed by {interaction.user.display_name}"

            await interaction.response.edit_message(view=self)
            await interaction.channel.send(
                f"This ticket has been claimed by {interaction.user.mention}."
            )

async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
