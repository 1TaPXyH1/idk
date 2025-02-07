import discord
from discord.ext import commands
import motor.motor_asyncio
from datetime import datetime, timedelta
import asyncio
from types import SimpleNamespace

class ClaimThread(commands.Cog):
    """
    Plugin to manage and track ticket claims
    """
    def __init__(self, bot):
        self.bot = bot
        
        # MongoDB setup
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            self.bot.config.get('mongodb_uri', 'mongodb://localhost:27017')
        )
        self.mongo_db = self.mongo_client[self.bot.config.get('mongodb_database', 'modmail')]
        
        # Collections
        self.ticket_stats_collection = self.mongo_db['ticket_stats']
        self.config_collection = self.mongo_db['plugin_configs']
        
        # Start background tasks
        self.bot.loop.create_task(self.background_ticket_state_check())
        self.bot.loop.create_task(self.background_thread_existence_check())

    async def background_ticket_state_check(self):
        """
        Periodic background task to verify and update ticket states
        """
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                # Find all non-closed tickets
                active_tickets = await self.ticket_stats_collection.find({
                    'current_state': {'$ne': 'closed'}
                }).to_list(length=None)
                
                for ticket in active_tickets:
                    try:
                        thread_id = int(ticket['thread_id'])
                        guild_id = int(ticket.get('guild_id', 0))
                        
                        # Find the guild
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            # If guild not found, mark as closed
                            await self.on_thread_state_change(
                                SimpleNamespace(id=thread_id, guild=None), 
                                'closed'
                            )
                            continue
                        
                        # Try to fetch the thread
                        thread = guild.get_thread(thread_id)
                        
                        # Check thread state
                        if thread is None or thread.closed:
                            # Dispatch closed state if thread is not found or closed
                            await self.on_thread_state_change(
                                thread or SimpleNamespace(id=thread_id, guild=guild), 
                                'closed'
                            )
                    
                    except Exception as ticket_error:
                        print(f"⚠️ Error checking ticket {ticket.get('thread_id', 'unknown')}: {ticket_error}")
            
            except Exception as e:
                print(f"❌ Background ticket state check failed: {e}")
            
            # Wait for 1 hour before next check
            await asyncio.sleep(3600)

    async def background_thread_existence_check(self):
        """
        Periodic background task to verify thread existence and update status
        Runs every 5 seconds to check active tickets
        """
        await self.bot.wait_until_ready()
        
        while not self.bot.is_closed():
            try:
                # Find all non-closed tickets
                active_tickets = await self.ticket_stats_collection.find({
                    'current_state': {'$ne': 'closed'}
                }).to_list(length=None)
                
                for ticket in active_tickets:
                    try:
                        thread_id = int(ticket['thread_id'])
                        guild_id = int(ticket.get('guild_id', 0))
                        
                        # Find the guild
                        guild = self.bot.get_guild(guild_id)
                        if not guild:
                            # If guild not found, mark as closed
                            await self.on_thread_state_change(
                                SimpleNamespace(id=thread_id, guild=None), 
                                'closed'
                            )
                            continue
                        
                        # Try to fetch the thread
                        thread = guild.get_thread(thread_id)
                        
                        # Check thread state
                        if thread is None or thread.closed:
                            # Dispatch closed state if thread is not found or closed
                            await self.on_thread_state_change(
                                thread or SimpleNamespace(id=thread_id, guild=guild), 
                                'closed'
                            )
                    
                    except Exception as ticket_error:
                        print(f"⚠️ Error checking ticket {ticket.get('thread_id', 'unknown')}: {ticket_error}")
            
            except Exception as e:
                print(f"❌ Background thread existence check failed: {e}")
            
            # Wait for 5 seconds before next check
            await asyncio.sleep(5)

    async def on_thread_state_change(self, thread, state, user=None):
        """
        Centralized event listener for ticket state changes
        
        Args:
            thread: The Discord thread
            state: New state of the ticket ('claimed', 'unclaimed', 'closed')
            user: User who triggered the state change (optional)
        """
        try:
            # Prepare state document
            stats_doc = {
                'thread_id': str(thread.id) if thread else 'unknown',
                'guild_id': str(thread.guild.id) if thread and thread.guild else 'unknown',
                'current_state': state,
                'status': 'closed' if state == 'closed' else 'open',
                'is_closed': state == 'closed',
                'last_updated': datetime.utcnow(),
                'closed_at': datetime.utcnow() if state == 'closed' else None,
                'last_user_id': str(user.id) if user else None,
                'moderator_id': str(user.id) if user else None
            }
            
            # Upsert ticket stats
            result = await self.ticket_stats_collection.update_one(
                {'thread_id': stats_doc['thread_id']},
                {'$set': stats_doc},
                upsert=True
            )
            
            # Minimal logging
            print(f"📋 Ticket State: {stats_doc['thread_id']} -> {state}")
            
        except Exception as e:
            print(f"❌ Error tracking ticket state: {e}")
            import traceback
            traceback.print_exc()

    @commands.check
    async def is_in_thread(ctx):
        """
        Check if the command is being used in a thread channel
        
        Args:
            ctx: The command context
        
        Returns:
            bool: True if in a thread, False otherwise
        """
        # Check if the channel is a thread
        if not isinstance(ctx.channel, discord.Thread):
            raise commands.CheckFailure("This command can only be used in a thread.")
        return True

    @commands.command(name="claim")
    @commands.check(is_in_thread)
    async def claim_thread(self, ctx):
        """
        Claim the current ticket thread
        """
        # Get the current thread
        thread = ctx.channel

        # Dispatch state change event
        await self.on_thread_state_change(thread, 'claimed', ctx.author)

        await ctx.send(f"✅ {ctx.author.mention} has acknowledged this ticket.")

    @commands.command(name="unclaim")
    @commands.check(is_in_thread)
    async def unclaim_thread(self, ctx):
        """
        Unclaim the current ticket thread
        """
        # Get the current thread
        thread = ctx.channel

        # Check if thread is closed
        is_thread_closed = thread.closed if hasattr(thread, 'closed') else False

        # Dispatch state change event
        await self.on_thread_state_change(thread, 'unclaimed', ctx.author)

        await ctx.send(f"✅ Ticket status reset.")

    @commands.command(name="thread_claim")
    @commands.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_claim(self, ctx):
        """
        Claim the current thread
        Prevents other supporters from replying
        """
        try:
            # Check claim limits
            config = await self.get_config()
            user_claims = await self.count_active_claims(ctx.author.id)
            
            if user_claims >= config.get('claim_limit', 5):
                await ctx.send(f"You have reached the maximum claim limit of {config['claim_limit']} threads.")
                return
            
            await ctx.send(f"Thread claimed by {ctx.author.mention}")
        
        except Exception as e:
            await ctx.send(f"Error claiming thread: {e}")
            print(f"Claim thread error: {e}")

    async def count_active_claims(self, user_id):
        """
        Count active claims for a specific user
        
        :param user_id: ID of the user
        :return: Number of active claims
        """
        try:
            # Count active claims using ticket stats collection
            active_claims = await self.ticket_stats_collection.count_documents({
                'last_user_id': str(user_id),
                'current_state': {'$ne': 'closed'}
            })
            return active_claims
        except Exception as e:
            print(f"❌ Error counting active claims: {e}")
            return 0

    @commands.command(name="thread_unclaim")
    @commands.has_permissions(PermissionLevel.SUPPORTER)
    async def thread_unclaim(self, ctx):
        """
        Unclaim the current thread
        Allows other supporters to claim and reply
        """
        try:
            await ctx.send(f"Thread unclaimed by {ctx.author.mention}")
        
        except Exception as e:
            await ctx.send(f"Error unclaiming thread: {e}")
            print(f"Unclaim thread error: {e}")

    @commands.command(name="set_claim_limit")
    @commands.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def set_claim_limit(self, ctx, limit: int):
        """
        Set the maximum number of threads a supporter can claim
        
        :param limit: Maximum number of active claims
        """
        if limit < 0:
            await ctx.send("Claim limit cannot be negative.")
            return
        
        await self.config_collection.update_one(
            {'_id': 'claim_config'},
            {'$set': {'claim_limit': limit}},
            upsert=True
        )
        
        await ctx.send(f"Claim limit set to {limit} threads.")

    async def get_config(self):
        """
        Retrieve plugin configuration
        
        :return: Configuration dictionary
        """
        config = await self.config_collection.find_one({'_id': 'claim_config'}) or {}
        return {
            'claim_limit': config.get('claim_limit', 5),
            'override_roles': config.get('override_roles', [])
        }

def setup(bot):
    """
    Setup function for Modmail plugin
    
    :param bot: Modmail bot instance
    """
    return ClaimThread(bot)
