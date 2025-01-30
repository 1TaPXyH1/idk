from .enhanced_logger import EnhancedLogger

async def setup(bot):
    await bot.add_cog(EnhancedLogger(bot))
