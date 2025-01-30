import discord
from discord.ext import commands
from datetime import datetime, timedelta
from core import checks
from core.models import PermissionLevel
import typing
from collections import defaultdict

class EnhancedLogger(commands.Cog):
    """Advanced logging and analytics system for Modmail"""
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.log_cache = {}
        self.analytics_cache = {}

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Initialize thread tracking"""
        try:
            if thread and hasattr(thread, 'channel'):
                await self.db.insert_one({
                    'thread_id': str(thread.id),
                    'channel_id': str(thread.channel.id),
                    'creator_id': str(creator.id),
                    'creator_name': str(creator),
                    'created_at': datetime.utcnow(),
                    'claimed_by': None,
                    'claim_time': None,
                    'status': 'open'
                })
                print(f"New ticket created by {creator} in channel {thread.channel.id}")
        except Exception as e:
            print(f"Error initializing thread: {e}")

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, reply, creator, message, anonymous):
        """Track message exchanges and response times"""
        try:
            if reply and creator:
                current_time = datetime.utcnow()
                # Better staff detection - check if member has guild roles
                is_staff = isinstance(creator, discord.Member)
                
                # Get existing ticket
                ticket = await self.db.find_one({'thread_id': str(thread.id)})
                if ticket:
                    # Add creator to handlers if they're staff and not already listed
                    if is_staff and str(creator.id) not in ticket.get('handlers', []):
                        await self.db.update_one(
                            {'thread_id': str(thread.id)},
                            {'$push': {'handlers': str(creator.id)}}
                        )
                
                print(f"Staff interaction logged for ticket {thread.id} by {creator}")
        except Exception as e:
            print(f"Error logging interaction: {e}")

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, time):
        """Enhanced log message when thread is closed"""
        try:
            if not thread or not hasattr(thread, 'channel'):
                print("No valid thread or channel found")
                return

            thread_data = await self.db.find_one({'thread_id': str(thread.id)})
            if not thread_data:
                print(f"No thread data found for {thread.id}")
                return

            # Find the log message in the channel
            log_message = None
            
            try:
                async for msg in thread.channel.history(limit=10):
                    if msg.embeds and any("Log" in embed.title for embed in msg.embeds):
                        log_message = msg
                        break
            except discord.NotFound:
                print(f"Channel not found for thread {thread.id}")
                return
            except Exception as e:
                print(f"Error searching channel history: {e}")
                return

            if log_message:
                # Create enhanced embed
                embed = discord.Embed(
                    title="📝 Enhanced Ticket Log",
                    description="Additional ticket information",
                    color=self.bot.main_color,
                    timestamp=datetime.utcnow()
                )

                # Basic ticket info
                embed.add_field(
                    name="Ticket Information",
                    value=f"**Creator:** {thread_data['creator_name']}\n"
                          f"**Created:** {thread_data['created_at'].strftime('%Y-%m-%d %H:%M:%S')}",
                    inline=False
                )

                # Claim information
                claimed_by = thread_data.get('claimed_by', 'Not Claimed')
                if claimed_by != 'Not Claimed':
                    claim_time = thread_data.get('claim_time', datetime.utcnow())
                    embed.add_field(
                        name="Claim Information",
                        value=f"**Claimed By:** {claimed_by}\n"
                              f"**Claimed At:** {claim_time.strftime('%Y-%m-%d %H:%M:%S')}",
                        inline=False
                    )

                # Closure information
                close_reason = message if message else 'No reason provided'
                embed.add_field(
                    name="Closure Information",
                    value=f"**Closed By:** {closer}\n"
                          f"**Close Reason:** {close_reason}",
                    inline=False
                )

                try:
                    await log_message.reply(embed=embed)
                    print(f"Enhanced log created for ticket {thread.id}")
                except Exception as e:
                    print(f"Error sending enhanced log: {e}")

        except Exception as e:
            print(f"Error creating enhanced log: {e}")

    @commands.Cog.listener()
    async def on_thread_claim(self, thread, user):
        """Track when someone claims a ticket"""
        try:
            if thread and user:
                await self.db.update_one(
                    {'thread_id': str(thread.id)},
                    {'$set': {
                        'claimed_by': str(user),
                        'claim_time': datetime.utcnow()
                    }}
                )
                print(f"Ticket {thread.id} claimed by {user}")
        except Exception as e:
            print(f"Error tracking claim: {e}")

    def calculate_avg_response_time(self, messages):
        """Calculate average response time between messages"""
        if not messages or len(messages) < 2:
            return 0
            
        response_times = []
        last_msg = None
        
        for msg in messages:
            if last_msg:
                if msg['is_staff'] != last_msg['is_staff']:  # Different author types
                    time_diff = (msg['timestamp'] - last_msg['timestamp']).total_seconds() / 60
                    response_times.append(time_diff)
            last_msg = msg
            
        return sum(response_times) / len(response_times) if response_times else 0

    def calculate_avg_handle_time(self, tickets):
        """Calculate average handling time for tickets"""
        handle_times = [t.get('resolution_time', 0) for t in tickets if 'resolution_time' in t]
        return sum(handle_times) / len(handle_times) if handle_times else 0

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ticketstats(self, ctx, days: int = 7):
        """View ticket statistics for the specified period"""
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            print(f"Fetching stats from {start_date}")
            
            cursor = self.db.find({
                'created_at': {'$gte': start_date}
            })
            
            tickets = await cursor.to_list(None)
            print(f"Found {len(tickets)} tickets")
            
            if not tickets:
                return await ctx.send(f"No tickets found in the last {days} days.")
            
            embed = discord.Embed(
                title=f"Ticket Statistics (Last {days} days)",
                color=self.bot.main_color,
                timestamp=ctx.message.created_at
            )
            
            # Basic Stats
            total = len(tickets)
            closed = sum(1 for t in tickets if t.get('status') == 'closed')
            open_tickets = total - closed
            
            embed.add_field(
                name="Overview",
                value=f"Total Tickets: {total}\n"
                      f"Open: {open_tickets}\n"
                      f"Closed: {closed}\n"
                      f"Close Rate: {(closed/total)*100:.1f}%",
                inline=False
            )
            
            # Response Times
            closed_tickets = [t for t in tickets if t.get('status') == 'closed']
            if closed_tickets:
                times = [t.get('resolution_time', 0) for t in closed_tickets]
                avg_time = sum(times) / len(times)
                
                # Convert to appropriate time format
                def format_time(minutes):
                    if minutes < 1:
                        return f"{minutes * 60:.0f} seconds"
                    return f"{minutes:.1f} minutes"
                
                embed.add_field(
                    name="Resolution Times",
                    value=f"Average: {format_time(avg_time)}\n"
                          f"Fastest: {format_time(min(times))}\n"
                          f"Slowest: {format_time(max(times))}",
                    inline=False
                )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Error retrieving statistics: {e}")

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def mystats(self, ctx, days: int = 30):
        """View your personal ticket handling statistics"""
        try:
            start_date = datetime.utcnow() - timedelta(days=days)
            print(f"Fetching stats for {ctx.author}")
            
            # Get tickets where user was a handler or closed them
            cursor = self.db.find({
                '$or': [
                    {'handlers': str(ctx.author.id)},
                    {'closed_by': str(ctx.author.id)}
                ],
                'created_at': {'$gte': start_date}
            })
            
            tickets = await cursor.to_list(None)
            print(f"Found {len(tickets)} tickets for {ctx.author}")
            
            if not tickets:
                return await ctx.send(f"No ticket activity found in the last {days} days.")
            
            embed = discord.Embed(
                title=f"Your Ticket Stats (Last {days} days)",
                color=self.bot.main_color,
                timestamp=ctx.message.created_at
            )
            
            # Count handled and closed tickets
            handled_tickets = len(tickets)
            closed_tickets = sum(1 for t in tickets 
                               if t.get('status') == 'closed' 
                               and t.get('closed_by') == str(ctx.author.id))
            
            embed.add_field(
                name="Activity Overview",
                value=f"Tickets Handled: {handled_tickets}\n"
                      f"Tickets Closed: {closed_tickets}",
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Error retrieving your statistics: {e}")

async def setup(bot):
    await bot.add_cog(EnhancedLogger(bot))
