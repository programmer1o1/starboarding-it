# starboarding-it

the stars of all time lol

the tutorial is simple: just react with ⭐ on a message. once enough different people do it, the bot reposts it as an embed in your starboard channel (or thread) and keeps the star count updated.

## Setup

1. make a bot at https://discord.com/developers/applications (new application, then bot, then reset token)
2. on the same page, turn on **message content intent**
3. invite it to your server with this link (swap in your app id):
   `https://discord.com/oauth2/authorize?client_id=your_app_id&scope=bot&permissions=274878024704`
4. run this:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

5. open `.env` and fill in your token + the starboard channel id
6. start it:

```
.venv/bin/python bot.py
```

on windows use `python bot.py` instead.

## Settings (.env)

- `discord_token`: your bot token
- `starboard_channel_id`: where the posts go (channel or thread)
- `threshold`: how many stars it takes (set it to 1 when testing)
- `star_emoji`: defaults to ⭐
- `allow_self_star`: let people star their own messages, default false
- `max_age_days`: ignore messages older than this, default 7

this is very simple right now but it should work tbh

## todo

- remove the post when stars drop below the threshold, or when the original gets deleted
- `/starboard top` command that lists the most starred messages sorta like how LOVEGOD did it
- ignore list for channels like #spam

## links

- discord.py docs: https://discordpy.readthedocs.io/en/stable/
- reaction events: https://discordpy.readthedocs.io/en/stable/api.html#discord.on_raw_reaction_add
- intents: https://discordpy.readthedocs.io/en/stable/intents.html
- discord developer portal: https://discord.com/developers/applications
- discord api docs: https://discord.com/developers/docs/intro
- permissions: https://discord.com/developers/docs/topics/permissions
- python-dotenv: https://github.com/theskumar/python-dotenv
- sqlite3 (python): https://docs.python.org/3/library/sqlite3.html
