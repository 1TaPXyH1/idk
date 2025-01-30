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
        """Log new ticket creation"""
        try:
            if thread and creator:
                current_time = datetime.utcnow()
                # Check if ticket already exists
                existing_ticket = await self.db.find_one({'thread_id': str(thread.id)})
                if not existing_ticket:
                    await self.db.insert_one({
                        'thread_id': str(thread.id),
                        'channel_id': str(thread.channel.id),
                        'creator_id': str(creator.id),
                        'creator_name': str(creator),
                        'created_at': current_time,
                        'status': 'open',
                        'messages': [],
                        'response_times': [],
                        'handlers': []
                    })
                    print(f"New ticket logged: {thread.id} by {creator}")
        except Exception as e:
            print(f"Error logging thread creation: {e}")

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
        """Track thread closure"""
        try:
            if thread and closer:
                current_time = datetime.utcnow()
                thread_data = await self.db.find_one({'thread_id': str(thread.id)})
                
                if thread_data:
                    created_at = thread_data.get('created_at', current_time)
                    # Calculate resolution time in minutes with decimal precision
                    delta = current_time - created_at
                    resolution_time = delta.total_seconds() / 60.0  # Convert to minutes
                    
                    await self.db.update_one(
                        {'thread_id': str(thread.id)},
                        {'$set': {
                            'closed_by': str(closer.id),
                            'closer_name': str(closer),
                            'closed_at': current_time,
                            'status': 'closed',
                            'close_message': str(message) if message else "No message provided",
                            'resolution_time': resolution_time
                        }}
                    )
                    print(f"Ticket {thread.id} closed by {closer} after {resolution_time:.2f} minutes")
        except Exception as e:
            print(f"Error logging thread closure: {e}")

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
