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
    async def on_thread_create(self, thread):
        """Log new ticket creation"""
        await self.db.insert_one({
            'thread_id': str(thread.id),
            'creator_id': str(thread.recipient.id),
            'created_at': datetime.utcnow(),
            'status': 'open',
            'messages': [],
            'response_times': [],
            'handlers': []
        })

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, message):
        """Track message exchanges and response times"""
        await self.db.update_one(
            {'thread_id': str(thread.id)},
            {'$push': {
                'messages': {
                    'author_id': str(message.author.id),
                    'content': message.content,
                    'timestamp': message.created_at,
                    'is_staff': message.author.id != thread.recipient.id
                }
            }}
        )

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, logs):
        """Track thread closure"""
        await self.db.update_one(
            {'thread_id': str(thread.id)},
            {'$set': {
                'closed_by': str(closer.id),
                'closed_at': datetime.utcnow(),
                'status': 'closed',
                'resolution_time': (datetime.utcnow() - thread.created_at).total_seconds() / 60
            }}
        )

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
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def viewlog(self, ctx, thread_id: str = None):
        """View detailed log of a ticket"""
        if not thread_id:
            thread_id = str(ctx.channel.id)
            
        log = await self.db.find_one({'thread_id': thread_id})
        if not log:
            return await ctx.send("No log found for this ticket.")
            
        embed = discord.Embed(
            title=f"Ticket Log #{thread_id}",
            color=self.bot.main_color
        )
        
        # Basic Info
        embed.add_field(
            name="Created",
            value=f"<t:{int(log['created_at'].timestamp())}:R>",
            inline=True
        )
        
        # Handler Info
        handlers = log.get('handlers', [])
        if handlers:
            handler_text = "\n".join(f"• {h['name']} (<t:{int(h['claimed_at'].timestamp())}:R>)" 
                                   for h in handlers)
            embed.add_field(
                name="Handlers",
                value=handler_text,
                inline=False
            )
        
        # Response Stats
        messages = log.get('messages', [])
        if messages:
            staff_msgs = sum(1 for m in messages if m['is_staff'])
            user_msgs = len(messages) - staff_msgs
            avg_response = self.calculate_avg_response_time(messages)
            
            embed.add_field(
                name="Message Stats",
                value=f"Staff Messages: {staff_msgs}\n"
                      f"User Messages: {user_msgs}\n"
                      f"Avg Response Time: {avg_response:.1f}min",
                inline=False
            )
        
        # Status Info
        status = log.get('status', 'open')
        if status == 'closed':
            closed_time = log.get('closed_at')
            if closed_time:
                embed.add_field(
                    name="Closed",
                    value=f"<t:{int(closed_time.timestamp())}:R>",
                    inline=True
                )
                
            resolution_time = log.get('resolution_time')
            if resolution_time:
                embed.add_field(
                    name="Resolution Time",
                    value=f"{resolution_time:.1f} minutes",
                    inline=True
                )
        
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def ticketstats(self, ctx, days: int = 7):
        """View ticket statistics for the specified period"""
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Get tickets in date range
        tickets = await self.db.find({
            'created_at': {'$gte': start_date}
        }).to_list(None)
        
        embed = discord.Embed(
            title=f"Ticket Statistics (Last {days} days)",
            color=self.bot.main_color
        )
        
        if not tickets:
            embed.description = "No tickets found in the specified time period."
            return await ctx.send(embed=embed)
        
        # Basic Stats
        total_tickets = len(tickets)
        closed_tickets = sum(1 for t in tickets if t.get('status') == 'closed')
        avg_time = self.calculate_avg_handle_time(tickets)
        
        embed.add_field(
            name="Overview",
            value=f"Total Tickets: {total_tickets}\n"
                  f"Resolved: {closed_tickets}\n"
                  f"Resolution Rate: {(closed_tickets/total_tickets)*100:.1f}%\n"
                  f"Avg Handle Time: {avg_time:.1f}min",
            inline=False
        )
        
        # Top Handlers
        handler_stats = {}
        for ticket in tickets:
            for handler in ticket.get('handlers', []):
                handler_stats[handler['name']] = handler_stats.get(handler['name'], 0) + 1
        
        top_handlers = sorted(handler_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        if top_handlers:
            embed.add_field(
                name="Top Handlers",
                value="\n".join(f"• {name}: {count} tickets" for name, count in top_handlers),
                inline=False
            )
        
        # Response Time Analysis
        response_times = []
        for ticket in tickets:
            messages = ticket.get('messages', [])
            if messages:
                avg_response = self.calculate_avg_response_time(messages)
                if avg_response > 0:
                    response_times.append(avg_response)
        
        if response_times:
            avg_response = sum(response_times) / len(response_times)
            embed.add_field(
                name="Response Times",
                value=f"Average: {avg_response:.1f}min\n"
                      f"Fastest: {min(response_times):.1f}min\n"
                      f"Slowest: {max(response_times):.1f}min",
                inline=False
            )
        
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def mystats(self, ctx, days: int = 30):
        """View your personal ticket handling statistics"""
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Get tickets handled by the user
        tickets = await self.db.find({
            'handlers.id': str(ctx.author.id),
            'created_at': {'$gte': start_date}
        }).to_list(None)
        
        embed = discord.Embed(
            title=f"Your Ticket Stats (Last {days} days)",
            color=self.bot.main_color
        )
        
        if not tickets:
            embed.description = "No tickets found in the specified time period."
            return await ctx.send(embed=embed)
        
        # Calculate stats
        total_handled = len(tickets)
        closed_tickets = sum(1 for t in tickets if t.get('status') == 'closed')
        avg_time = self.calculate_avg_handle_time(tickets)
        
        embed.add_field(
            name="Your Overview",
            value=f"Tickets Handled: {total_handled}\n"
                  f"Resolved: {closed_tickets}\n"
                  f"Resolution Rate: {(closed_tickets/total_handled)*100:.1f}%\n"
                  f"Avg Handle Time: {avg_time:.1f}min",
            inline=False
        )
        
        # Response time analysis
        response_times = []
        total_messages = 0
        for ticket in tickets:
            messages = ticket.get('messages', [])
            if messages:
                staff_messages = sum(1 for m in messages if m['is_staff'] and m['author_id'] == str(ctx.author.id))
                total_messages += staff_messages
                avg_response = self.calculate_avg_response_time(messages)
                if avg_response > 0:
                    response_times.append(avg_response)
        
        if response_times:
            avg_response = sum(response_times) / len(response_times)
            embed.add_field(
                name="Your Response Stats",
                value=f"Total Messages: {total_messages}\n"
                      f"Avg Response Time: {avg_response:.1f}min\n"
                      f"Best Response Time: {min(response_times):.1f}min",
                inline=False
            )
        
        await ctx.send(embed=embed)
