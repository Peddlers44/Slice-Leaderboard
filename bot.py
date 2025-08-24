import os
import asyncio
from typing import Optional, List

import discord
from discord.ext import commands

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    UniqueConstraint,
    select,
    desc,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import create_engine

# (Optional) load .env locally if available
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
DATABASE_URL = os.getenv("DATABASE_URL")  # Render provides this automatically if you add Postgres

# Fallback to local SQLite if DATABASE_URL not set (useful for testing)
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///leaderboard.db"

# Render Postgres fix (psycopg2 dialect)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

PREFIX = "!"

Base = declarative_base()
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# ================== MODEL ==================
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    guild_id = Column(BigInteger, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    display_name = Column(String(128), nullable=False, default="Unknown")
    count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_guild_user"),
    )


def init_db():
    Base.metadata.create_all(engine)


# ================== DB HELPERS ==================
def _add_count(guild_id: int, user_id: int, delta: int, display_name: str) -> int:
    with SessionLocal() as s:
        row = s.execute(
            select(Order).where(Order.guild_id == guild_id, Order.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            row = Order(guild_id=guild_id, user_id=user_id, display_name=display_name, count=0)
            s.add(row)
        row.count = max(0, (row.count or 0) + delta)
        if display_name and row.display_name != display_name:
            row.display_name = display_name
        s.commit()
        return row.count


def _set_count(guild_id: int, user_id: int, value: int, display_name: str) -> int:
    with SessionLocal() as s:
        row = s.execute(
            select(Order).where(Order.guild_id == guild_id, Order.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            row = Order(guild_id=guild_id, user_id=user_id, display_name=display_name, count=0)
            s.add(row)
        row.count = max(0, int(value))
        if display_name and row.display_name != display_name:
            row.display_name = display_name
        s.commit()
        return row.count


def _remove_user(guild_id: int, user_id: int) -> bool:
    with SessionLocal() as s:
        row = s.execute(
            select(Order).where(Order.guild_id == guild_id, Order.user_id == user_id)
        ).scalar_one_or_none()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def _reset_all(guild_id: int) -> int:
    with SessionLocal() as s:
        rows = s.execute(select(Order).where(Order.guild_id == guild_id)).scalars().all()
        n = 0
        for r in rows:
            r.count = 0
            n += 1
        s.commit()
        return n


def _top_n(guild_id: int, n: int = 10) -> List[Order]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Order).where(Order.guild_id == guild_id).order_by(desc(Order.count), Order.user_id).limit(n)
        ).scalars().all()
        return rows


# ================== DISCORD BOT ==================
bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS, help_command=None)

EMOJI_MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


def name_for(member: Optional[discord.Member], fallback: str) -> str:
    if member:
        return member.display_name
    return fallback or "Unknown"


# ---------- Role-based checks ----------
def _member_has_any_roles(member: discord.Member, role_names: List[str]) -> bool:
    wanted = {n.lower() for n in role_names}
    current = {r.name.lower() for r in member.roles}
    return not current.isdisjoint(wanted)


def has_any_named_roles(*role_names: str):
    """Allow command only if user has at least one of the given role names."""
    async def predicate(ctx: commands.Context):
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.reply("This command can only be used in a server.", mention_author=False)
            return False
        if not _member_has_any_roles(ctx.author, list(role_names)):
            pretty = " or ".join(role_names)
            await ctx.reply(f"You need the **{pretty}** role to use this command.", mention_author=False)
            return False
        return True
    return commands.check(predicate)


@bot.event
async def on_ready():
    init_db()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print("Leaderboard bot is ready.")


@bot.command(name="add")
@has_any_named_roles("Chef")  # Require Chef role
async def add_cmd(ctx: commands.Context):
    guild = ctx.guild
    if guild is None:
        return await ctx.reply("This command can only be used in a server.", mention_author=False)

    member: discord.Member = ctx.author
    display = name_for(member, member.name)
    new_count = await asyncio.to_thread(_add_count, guild.id, member.id, 1, display)
    await ctx.reply(f"✅ Order complete, you now have **{new_count}** orders completed.", mention_author=False)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard_cmd(ctx: commands.Context):
    guild = ctx.guild
    if guild is None:
        return await ctx.reply("This command can only be used in a server.", mention_author=False)

    rows = await asyncio.to_thread(_top_n, guild.id, 10)
    if not rows:
        return await ctx.reply("No data yet. Use `!add` to get started!", mention_author=False)

    lines = []
    for idx, row in enumerate(rows):
        medal = EMOJI_MEDALS.get(idx, f"{idx+1}.")
        member = guild.get_member(row.user_id)
        display = name_for(member, row.display_name)
        lines.append(f"{medal} **{display}** — **{row.count}**")

    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="remove")
@has_any_named_roles("Head Chef", "Owner")  # only Head Chef or Owner
async def remove_cmd(ctx: commands.Context, user: Optional[discord.Member] = None):
    if ctx.guild is None:
        return await ctx.reply("This command can only be used in a server.", mention_author=False)
    if user is None:
        return await ctx.reply(f"Usage: `{PREFIX}remove @user`", mention_author=False)

    ok = await asyncio.to_thread(_remove_user, ctx.guild.id, user.id)
    if ok:
        await ctx.reply(f"🗑️ Removed **{name_for(user, user.name)}** from the leaderboard.", mention_author=False)
    else:
        await ctx.reply("That user wasn’t on the leaderboard.", mention_author=False)


@bot.command(name="set")
@has_any_named_roles("Head Chef", "Owner")  # only Head Chef or Owner
async def set_cmd(ctx: commands.Context, user: Optional[discord.Member] = None, amount: Optional[int] = None):
    if ctx.guild is None:
        return await ctx.reply("This command can only be used in a server.", mention_author=False)
    if user is None or amount is None:
        return await ctx.reply(f"Usage: `{PREFIX}set @user <amount>`", mention_author=False)
    if amount < 0:
        return await ctx.reply("Amount cannot be negative.", mention_author=False)

    new_val = await asyncio.to_thread(_set_count, ctx.guild.id, user.id, amount, name_for(user, user.name))
    await ctx.reply(f"✏️ Set **{name_for(user, user.name)}** to **{new_val}** orders.", mention_author=False)


@bot.command(name="resetall")
@has_any_named_roles("Head Chef", "Owner")  # only Head Chef or Owner
async def resetall_cmd(ctx: commands.Context):
    if ctx.guild is None:
        return await ctx.reply("This command can only be used in a server.", mention_author=False)
    n = await asyncio.to_thread(_reset_all, ctx.guild.id)
    await ctx.reply(f"♻️ Reset **{n}** users to **0** orders.", mention_author=False)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable.")
    bot.run(BOT_TOKEN)