# Credits and orignal author: https://github.com/fourjr/modmail-plugins/blob/master/claim/claim.py
# Enhanced version with additional features

import discord
from discord.ext import commands
import traceback
from datetime import datetime, timedelta
import asyncio

from core import checks
from core.models import PermissionLevel
from core.utils import match_user_id


class ClaimThread(commands.Cog):
    """Enhanced system for claiming and managing modmail threads"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        check_reply.fail_msg = 'This thread has been claimed by another user.'
        self.bot.get_command('reply').add_check(check_reply)
        self.bot.get_command('areply').add_check(check_reply)
        self.bot.get_command('fareply').add_check(check_reply)
        self.bot.get_command('freply').add_check(check_reply)
        
        # Initialize settings
        self.log_channel_id = None
        self.auto_unclaim_hours = 24  # Default to 24 hours
        self.notification_role_id = None
        
        # Start background tasks
        bot.loop.create_task(self.load_settings())
        bot.loop.create_task(self.auto_unclaim_loop())

    async def load_settings(self):
        """Load settings from database on startup"""
        await self.bot.wait_until_ready()
        config = await self.db.find_one({'_id': 'config'})
        if config:
            self.log_channel_id = config.get('log_channel_id')
            self.auto_unclaim_hours = config.get('auto_unclaim_hours', 24)
            self.notification_role_id = config.get('notification_role_id')

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
            
            embed.add_field(name="Action", value=action, inline=False)
            embed.add_field(name="Thread", value=ctx.channel.mention, inline=True)
            embed.add_field(name="User", value=ctx.author.mention, inline=True)
            
            if target:
                embed.add_field(name="Target", value=target.mention, inline=True)
            
            if error:
                embed.add_field(name="Error", value=f"```{str(error)}```", inline=False)
                
            embed.set_footer(text=f"Thread ID: {ctx.channel.id}")
            await log_channel.send(embed=embed)
            
        except Exception as e:
            print(f"Logging error: {str(e)}")

    async def auto_unclaim_loop(self):
        """Background task to automatically unclaim inactive threads"""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                cursor = self.db.find({'guild': str(self.bot.modmail_guild.id)})
                async for thread_data in cursor:
                    if 'claimers' in thread_data and thread_data['claimers']:
                        channel = self.bot.get_channel(int(thread_data['thread_id']))
                        if channel:
                            last_message = await channel.history(limit=1).flatten()
                            if last_message:
                                time_diff = datetime.utcnow() - last_message[0].created_at
                                if time_diff > timedelta(hours=self.auto_unclaim_hours):
                                    await self.auto_unclaim_thread(channel, thread_data)
            except Exception as e:
                print(f"Auto-unclaim error: {str(e)}")
            await asyncio.sleep(3600)  # Check every hour

    async def auto_unclaim_thread(self, channel, thread_data):
        """Automatically unclaim an inactive thread"""
        await self.db.find_one_and_update(
            {'thread_id': thread_data['thread_id']},
            {'$set': {'claimers': []}}
        )
        
        embed = discord.Embed(
            title="Thread Auto-Unclaimed",
            description=f"This thread was automatically unclaimed due to {self.auto_unclaim_hours} hours of inactivity.",
            color=discord.Color.orange()
        )
        await channel.send(embed=embed)
        
        # Rename channel back
        try:
            original_name = channel.name.split('-claimed-by-')[0]
            await channel.edit(name=original_name)
        except:
            pass

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
                            'claimers': [str(ctx.author.id)],
                            'claimed_at': datetime.utcnow().timestamp()
                        })
                    else:
                        await self.db.find_one_and_update(
                            {'thread_id': str(ctx.thread.channel.id), 'guild': str(self.bot.modmail_guild.id)}, 
                            {
                                '$addToSet': {'claimers': str(ctx.author.id)},
                                '$set': {'claimed_at': datetime.utcnow().timestamp()}
                            }
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
                    
                    # Notify support team if role is set
                    if self.notification_role_id:
                        role = ctx.guild.get_role(self.notification_role_id)
                        if role:
                            await ctx.send(f"{role.mention} New ticket claimed by {ctx.author.mention}")
                    
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
