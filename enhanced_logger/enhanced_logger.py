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
                await self.db.insert_one({
                    'thread_id': str(thread.id),
                    'creator_id': str(creator.id),
                    'creator_name': str(creator),
                    'created_at': datetime.utcnow(),
                    'status': 'open',
                    'messages': [],
                    'response_times': [],
                    'handlers': []
                })
        except Exception as e:
            print(f"Error logging thread creation: {e}")

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, reply, creator, message, anonymous):
        """Track message exchanges and response times"""
        try:
            if message and hasattr(message, 'content'):
                await self.db.update_one(
                    {'thread_id': str(thread.id)},
                    {'$push': {
                        'messages': {
                            'author_id': str(creator.id),
                            'author_name': str(creator),
                            'content': message.content,
                            'timestamp': datetime.utcnow(),
                            'is_staff': not isinstance(creator, discord.User)
                        }
                    }}
                )
        except Exception as e:
            print(f"Error logging message: {e}")

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, time):
        """Track thread closure"""
        try:
            if thread and closer:
                await self.db.update_one(
                    {'thread_id': str(thread.id)},
                    {'$set': {
                        'closed_by': str(closer.id),
                        'closer_name': str(closer),
                        'closed_at': datetime.utcnow(),
                        'status': 'closed',
                        'close_message': str(message) if message else "No message provided",
                        'resolution_time': (datetime.utcnow() - thread.created_at).total_seconds() / 60
                    }}
                )
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
            
            # Get tickets in date range
            cursor = self.db.find({
                'created_at': {'$gte': start_date}
            })
            
            tickets = await cursor.to_list(None)
            
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
                embed.add_field(
                    name="Resolution Times",
                    value=f"Average: {avg_time:.1f} minutes\n"
                          f"Fastest: {min(times):.1f} minutes\n"
                          f"Slowest: {max(times):.1f} minutes",
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
            
            # Get tickets where user sent messages
            cursor = self.db.find({
                'messages': {
                    '$elemMatch': {
                        'author_id': str(ctx.author.id),
                        'is_staff': True
                    }
                },
                'created_at': {'$gte': start_date}
            })
            
            tickets = await cursor.to_list(None)
            
            if not tickets:
                return await ctx.send(f"No ticket activity found in the last {days} days.")
            
            embed = discord.Embed(
                title=f"Your Ticket Stats (Last {days} days)",
                color=self.bot.main_color,
                timestamp=ctx.message.created_at
            )
            
            # Count messages
            total_messages = sum(
                sum(1 for m in t.get('messages', [])
                    if m.get('author_id') == str(ctx.author.id) and m.get('is_staff'))
                for t in tickets
            )
            
            # Count closed tickets
            closed_tickets = sum(1 for t in tickets if t.get('status') == 'closed' 
                               and t.get('closed_by') == str(ctx.author.id))
            
            embed.add_field(
                name="Activity Overview",
                value=f"Tickets Handled: {len(tickets)}\n"
                      f"Messages Sent: {total_messages}\n"
                      f"Tickets Closed: {closed_tickets}",
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Error retrieving your statistics: {e}")

async def setup(bot):
    await bot.add_cog(EnhancedLogger(bot))
