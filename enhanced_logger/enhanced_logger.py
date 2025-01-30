class EnhancedLogger(commands.Cog):
    """Advanced logging and analytics system for Modmail"""
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.api.get_plugin_partition(self)
        self.log_cache = {}
        self.analytics_cache = {}
        
    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs(self, ctx, user: typing.Union[discord.Member, discord.User] = None):
        """View logs for a user or your own handled tickets"""
        if user is None:
            user = ctx.author
            
        logs = await self.db.find({'handler_id': str(user.id)}).to_list(None)
        
        embed = discord.Embed(
            title=f"Logs for {user.display_name}",
            color=self.bot.main_color
        )
        
        if logs:
            # Group logs by status
            active = [log for log in logs if log.get('status') != 'closed']
            closed = [log for log in logs if log.get('status') == 'closed']
            
            # Calculate metrics
            avg_response_time = self.calculate_response_time(logs)
            resolution_rate = len(closed) / len(logs) if logs else 0
            
            embed.add_field(
                name="Active Tickets",
                value=f"{len(active)} tickets",
                inline=True
            )
            embed.add_field(
                name="Resolved Tickets",
                value=f"{len(closed)} tickets",
                inline=True
            )
            embed.add_field(
                name="Resolution Rate",
                value=f"{resolution_rate:.1%}",
                inline=True
            )
            embed.add_field(
                name="Average Response Time",
                value=f"{avg_response_time:.1f} minutes",
                inline=True
            )
        else:
            embed.description = "No logs found"
            
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def analytics(self, ctx, days: int = 7):
        """View detailed analytics for the support system"""
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        logs = await self.db.find({
            'created_at': {'$gte': start_date, '$lte': end_date}
        }).to_list(None)
        
        embed = discord.Embed(
            title=f"Support Analytics - Last {days} days",
            color=self.bot.main_color
        )
        
        if logs:
            # Calculate metrics
            total_tickets = len(logs)
            resolved_tickets = len([log for log in logs if log.get('status') == 'closed'])
            avg_resolution_time = self.calculate_resolution_time(logs)
            peak_hours = self.analyze_peak_hours(logs)
            
            embed.add_field(
                name="Total Tickets",
                value=str(total_tickets),
                inline=True
            )
            embed.add_field(
                name="Resolved Tickets",
                value=str(resolved_tickets),
                inline=True
            )
            embed.add_field(
                name="Resolution Rate",
                value=f"{(resolved_tickets/total_tickets):.1%}",
                inline=True
            )
            embed.add_field(
                name="Avg Resolution Time",
                value=f"{avg_resolution_time:.1f} hours",
                inline=True
            )
            embed.add_field(
                name="Peak Hours",
                value=", ".join(f"{h}:00" for h in peak_hours[:3]),
                inline=True
            )
            
            # Add performance graph
            graph = await self.generate_performance_graph(logs)
            if graph:
                embed.set_image(url=graph)
        else:
            embed.description = "No data for the specified period"
            
        await ctx.send(embed=embed)

    def calculate_response_time(self, logs):
        """Calculate average response time"""
        times = []
        for log in logs:
            if 'messages' in log:
                messages = sorted(log['messages'], key=lambda x: x['timestamp'])
                for i in range(1, len(messages)):
                    if messages[i]['author'] != messages[i-1]['author']:
                        time_diff = messages[i]['timestamp'] - messages[i-1]['timestamp']
                        times.append(time_diff.total_seconds() / 60)
        return sum(times) / len(times) if times else 0

    def analyze_peak_hours(self, logs):
        """Analyze peak activity hours"""
        hour_counts = defaultdict(int)
        for log in logs:
            if 'created_at' in log:
                hour = log['created_at'].hour
                hour_counts[hour] += 1
        return sorted(hour_counts.keys(), key=lambda h: hour_counts[h], reverse=True)

    async def generate_performance_graph(self, logs):
        """Generate performance visualization"""
        # Implementation would use matplotlib or another graphing library
        # Return URL to generated graph image
        pass

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, logs):
        """Log thread closure and update analytics"""
        await self.db.insert_one({
            'thread_id': str(thread.id),
            'closer_id': str(closer.id),
            'logs': logs,
            'created_at': thread.created_at,
            'closed_at': datetime.utcnow(),
            'status': 'closed'
        })
