"""
what it does: react with a star on a message. once enough different people do it,
the bot reposts it as an embed in the starboard channel and keeps the count updated

run it:   .venv/bin/python bot.py
settings: .env (copy .env.example)

docs:
  discord.py        https://discordpy.readthedocs.io/en/stable/
  reaction events   https://discordpy.readthedocs.io/en/stable/api.html#discord.on_raw_reaction_add
  intents           https://discordpy.readthedocs.io/en/stable/intents.html
  discord api       https://discord.com/developers/docs/intro
"""
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import discord
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def norm(emoji):
    # the star can come with an invisible extra character, so remove it before comparing
    return emoji.replace("\ufe0f", "").strip()


# ---------- settings (all come from .env) ----------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEST_ID = int(os.getenv("STARBOARD_CHANNEL_ID") or 0)   # a text channel or a thread
THRESHOLD = int(os.getenv("THRESHOLD") or 3)            # stars needed
STAR = norm(os.getenv("STAR_EMOJI") or "⭐")             # custom emoji looks like <:name:id>
ALLOW_SELF_STAR = (os.getenv("ALLOW_SELF_STAR") or "false").lower() == "true"
MAX_AGE_DAYS = int(os.getenv("MAX_AGE_DAYS") or 7)      # older messages can't get on the board
DEBOUNCE_SECONDS = 2                                    # wait a bit so fast stars = one update
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

log = logging.getLogger("starboard")
NO_PINGS = discord.AllowedMentions.none()               # never ping anyone from a repost


# ---------- what a post looks like ----------
def star_icon(stars):
    return "🌟" if stars >= THRESHOLD * 2 else "⭐"


def header(stars, message):
    # the text above the embed. this is the part we edit when the count changes
    return f"{star_icon(stars)} **{stars}** | {message.channel.mention}"


async def build_post(message):
    # returns (embed, file or None)
    embed = discord.Embed(
        description=message.clean_content[:4000] or None,
        color=discord.Color.gold(),
        timestamp=message.created_at,
    )
    embed.set_author(name=message.author.display_name,
                     icon_url=message.author.display_avatar.url, url=message.jump_url)

    file, first = None, None
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/") and not a.is_spoiler()]
    if images:
        first = images[0]
        # re-upload the image because discord's attachment links expire
        try:
            if first.size <= MAX_UPLOAD_BYTES:
                file = await first.to_file(filename="star" + Path(first.filename).suffix)
                embed.set_image(url=f"attachment://{file.filename}")
        except discord.HTTPException:
            file = None
        if file is None:
            embed.set_image(url=first.url)  # fallback: link to the original

    others = [a for a in message.attachments if a is not first]
    if others:
        embed.add_field(name="Attachments", inline=False,
                        value="\n".join(f"[{a.filename}]({a.url})" for a in others)[:1024])
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url})", inline=False)
    return embed, file


# ---------- the bot ----------
class Starboard(discord.Client):
    # tip: don't name your own attributes emojis, users, guilds, user...
    # discord.Client already uses those names and python will complain

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # turn this on in the developer portal too
        super().__init__(intents=intents)
        # small database that remembers what's already on the board (so no double posts)
        self.store = sqlite3.connect(HERE / "starboard.db")
        self.store.execute("CREATE TABLE IF NOT EXISTS starred ("
                           "message_id INTEGER PRIMARY KEY, post_id INTEGER, stars INTEGER NOT NULL)")
        self.scheduled = {}          # message_id -> recount that's waiting
        self.write_lock = asyncio.Lock()
        self.dest_guild_id = None
        self.checked = False

    # startup checks
    async def on_ready(self):
        if self.checked:             # on_ready can run again after a reconnect
            return
        self.checked = True
        try:
            dest = await self.fetch_channel(DEST_ID)
        except (discord.NotFound, discord.Forbidden):
            log.error("Can't see STARBOARD_CHANNEL_ID %s. Invite the bot to that server first.", DEST_ID)
            return await self.close()
        if not isinstance(dest, (discord.TextChannel, discord.Thread)):
            log.error("STARBOARD_CHANNEL_ID must be a text channel or a thread.")
            return await self.close()
        self.dest_guild_id = dest.guild.id

        perms = dest.permissions_for(dest.guild.me)
        can_send = perms.send_messages_in_threads if isinstance(dest, discord.Thread) else perms.send_messages
        missing = [n for n, ok in (("Send Messages (in Threads)", can_send),
                                   ("Embed Links", perms.embed_links),
                                   ("Attach Files", perms.attach_files)) if not ok]
        if missing:
            log.warning("Bot is missing permissions in the starboard: %s", ", ".join(missing))
        log.info("Logged in as %s | board: #%s | needs %d x %s", self.user, dest.name, THRESHOLD, STAR)

    # step 1: someone adds or removes a reaction
    # (the "raw" events work even if the message isn't cached)
    def relevant(self, guild_id, channel_id):
        # only our server, and ignore the board itself
        return guild_id == self.dest_guild_id and channel_id != DEST_ID

    async def on_raw_reaction_add(self, payload):
        if (norm(str(payload.emoji)) == STAR and not payload.burst
                and self.relevant(payload.guild_id, payload.channel_id)):
            self.queue_recount(payload.channel_id, payload.message_id)

    async def on_raw_reaction_remove(self, payload):
        if (norm(str(payload.emoji)) == STAR and not payload.burst
                and self.relevant(payload.guild_id, payload.channel_id)):
            self.queue_recount(payload.channel_id, payload.message_id)

    async def on_raw_reaction_clear(self, payload):          # a mod removed all reactions
        if self.relevant(payload.guild_id, payload.channel_id):
            self.queue_recount(payload.channel_id, payload.message_id)

    async def on_raw_reaction_clear_emoji(self, payload):    # a mod removed one emoji
        if norm(str(payload.emoji)) == STAR and self.relevant(payload.guild_id, payload.channel_id):
            self.queue_recount(payload.channel_id, payload.message_id)

    # step 2: wait a moment so a bunch of stars becomes one update
    def queue_recount(self, channel_id, message_id):
        if message_id not in self.scheduled:  # one is already waiting, it'll catch this too
            self.scheduled[message_id] = asyncio.create_task(self.recount(channel_id, message_id))

    async def recount(self, channel_id, message_id):
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self.scheduled.pop(message_id, None)  # new reactions after this start a new recount
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            async with self.write_lock:           # one post/edit at a time, no duplicates
                await self.update_starboard(message)
        except discord.Forbidden:
            log.warning("Permission problem on message %s. Check the bot's permissions in the "
                        "starboard (Send Messages / Send Messages in Threads / Embed Links) "
                        "and that the thread isn't locked.", message_id)
        except discord.HTTPException as e:
            log.warning("Discord error on message %s: %s", message_id, e)
        except Exception:
            log.exception("Unexpected error on message %s", message_id)

    # step 3: count the stars
    # we recount from scratch every time instead of doing +1/-1, so it can't drift
    async def count_stars(self, message):
        voters = set()
        for reaction in message.reactions:
            if norm(str(reaction.emoji)) == STAR:
                async for user in reaction.users():
                    if not user.bot:
                        voters.add(user.id)
        if not ALLOW_SELF_STAR:
            voters.discard(message.author.id)
        return len(voters)

    # step 4: post it, or edit the post that's already there
    async def get_dest(self):
        dest = await self.fetch_channel(DEST_ID)   # fetch fresh so we know if a thread got archived
        if isinstance(dest, discord.Thread) and dest.archived:
            dest = await dest.edit(archived=False)  # can't post in an archived thread
        return dest

    async def update_starboard(self, message):
        if message.author.bot:
            return
        row = self.store.execute("SELECT post_id, stars FROM starred WHERE message_id = ?",
                                 (message.id,)).fetchone()
        if row is None and discord.utils.utcnow() - message.created_at > timedelta(days=MAX_AGE_DAYS):
            return
        stars = await self.count_stars(message)

        if row is None:                                     # not on the board yet
            if stars < THRESHOLD:
                return
            dest = await self.get_dest()
            embed, file = await build_post(message)
            extra = {"file": file} if file else {}
            post = await dest.send(header(stars, message), embed=embed,
                                   allowed_mentions=NO_PINGS, **extra)
            self.store.execute("INSERT INTO starred VALUES (?, ?, ?)", (message.id, post.id, stars))
            self.store.commit()
            log.info("posted %s stars: %s: %s", stars, message.author,
                     message.clean_content[:50].replace("\n", " "))

        elif stars != row[1]:                               # already on the board, count changed
            self.store.execute("UPDATE starred SET stars = ? WHERE message_id = ?", (stars, message.id))
            self.store.commit()
            try:
                dest = await self.get_dest()
                await dest.get_partial_message(row[0]).edit(content=header(stars, message),
                                                            allowed_mentions=NO_PINGS)
                log.info("updated to %s stars (message %s)", stars, message.id)
            except discord.NotFound:
                log.info("starboard post for %s was deleted; not reposting", message.id)


def main():
    if not TOKEN:
        sys.exit("DISCORD_TOKEN is empty. Fill in .env (copy .env.example).")
    if not DEST_ID:
        sys.exit("STARBOARD_CHANNEL_ID is empty. Fill in .env.")
    discord.utils.setup_logging(level=logging.INFO)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # hides the harmless PyNaCl warning
    try:
        Starboard().run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        sys.exit("Discord rejected the token. Reset it in the Developer Portal (Bot page).")
    except discord.PrivilegedIntentsRequired:
        sys.exit("Turn on MESSAGE CONTENT INTENT: Developer Portal -> Bot -> save, then restart.")


if __name__ == "__main__":
    main()
