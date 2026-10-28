import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

START_TIME = time.monotonic()
db = sqlite3.connect(os.getenv("AVLINK_DATABASE", "avlink.db"))
db.row_factory = sqlite3.Row
db.execute("PRAGMA busy_timeout = 5000")


# -------------------- database and migrations --------------------

def cols(table):
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


def migrate():
    db.execute("""CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER PRIMARY KEY,
        cooldown INTEGER NOT NULL DEFAULT 0,
        remove_after_use INTEGER NOT NULL DEFAULT 0,
        default_delivery TEXT NOT NULL DEFAULT 'dm',
        weekly_limit INTEGER NOT NULL DEFAULT 0,
        panel_enabled INTEGER NOT NULL DEFAULT 1
    )""")
    for name, definition in {
        "cooldown": "INTEGER NOT NULL DEFAULT 0",
        "remove_after_use": "INTEGER NOT NULL DEFAULT 0",
        "default_delivery": "TEXT NOT NULL DEFAULT 'dm'",
        "weekly_limit": "INTEGER NOT NULL DEFAULT 0",
        "panel_enabled": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if name not in cols("settings"):
            db.execute(f"ALTER TABLE settings ADD COLUMN {name} {definition}")

    db.execute("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        UNIQUE(guild_id, name)
    )""")

    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='links'").fetchone():
        db.execute("""CREATE TABLE links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            category_id INTEGER,
            UNIQUE(guild_id, url)
        )""")
    else:
        link_cols = cols("links")
        global_unique = False
        for idx in db.execute("PRAGMA index_list(links)").fetchall():
            if idx["unique"]:
                indexed = [r["name"] for r in db.execute(f"PRAGMA index_info('{idx['name']}')")]
                if indexed == ["url"]:
                    global_unique = True
        if "guild_id" not in link_cols or "category_id" not in link_cols or global_unique:
            old_rows = db.execute("SELECT * FROM links").fetchall()
            db.execute("ALTER TABLE links RENAME TO links_old_migration")
            db.execute("""CREATE TABLE links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                category_id INTEGER,
                UNIQUE(guild_id, url)
            )""")
            for row in old_rows:
                keys = set(row.keys())
                url = row["url"] if "url" in keys else None
                if not url:
                    continue
                old_id = row["id"] if "id" in keys else None
                gid = row["guild_id"] if "guild_id" in keys and row["guild_id"] else 0
                cid = row["category_id"] if "category_id" in keys else None
                db.execute("INSERT OR IGNORE INTO links(id,guild_id,url,category_id) VALUES(?,?,?,?)",
                           (old_id, gid, url, cid))
            db.execute("DROP TABLE links_old_migration")

    db.execute("""CREATE TABLE IF NOT EXISTS weekly_usage (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        week TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id, user_id, week)
    )""")
    db.commit()


migrate()


def ensure_settings(gid):
    db.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (gid,))
    db.commit()


def ensure_general(gid):
    db.execute("INSERT OR IGNORE INTO categories(guild_id,name) VALUES(?, 'general')", (gid,))
    db.commit()
    return db.execute("SELECT id FROM categories WHERE guild_id=? AND name='general'", (gid,)).fetchone()["id"]


def settings_for(gid):
    ensure_settings(gid)
    r = db.execute("SELECT * FROM settings WHERE guild_id=?", (gid,)).fetchone()
    return int(r["cooldown"] or 0), bool(r["remove_after_use"]), (r["default_delivery"] if r["default_delivery"] in ("dm", "private") else "dm"), int(r["weekly_limit"] or 0), bool(r["panel_enabled"])


def categories_for(gid):
    ensure_general(gid)
    return db.execute("SELECT id,name FROM categories WHERE guild_id=? ORDER BY name COLLATE NOCASE", (gid,)).fetchall()


def category_for(gid, name):
    return db.execute("SELECT id,name FROM categories WHERE guild_id=? AND name=? COLLATE NOCASE", (gid, name.strip().lower())).fetchone()


def links_for(gid, cid=None):
    if cid is None:
        return db.execute("SELECT id,url,category_id FROM links WHERE guild_id=? ORDER BY id", (gid,)).fetchall()
    return db.execute("SELECT id,url,category_id FROM links WHERE guild_id=? AND category_id=? ORDER BY id", (gid, cid)).fetchall()


def week_key():
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_used(gid, uid):
    r = db.execute("SELECT count FROM weekly_usage WHERE guild_id=? AND user_id=? AND week=?", (gid, uid, week_key())).fetchone()
    return int(r["count"]) if r else 0


def weekly_increment(gid, uid):
    db.execute("""INSERT INTO weekly_usage(guild_id,user_id,week,count) VALUES(?,?,?,1)
        ON CONFLICT(guild_id,user_id,week) DO UPDATE SET count=count+1""", (gid, uid, week_key()))
    db.commit()


def quota_text(gid, uid, after_delivery=False):
    _, _, _, limit, _ = settings_for(gid)
    used = weekly_used(gid, uid) + (1 if after_delivery else 0)
    if not limit:
        return f"weekly usage: {used} links used · no weekly limit"
    return f"weekly usage: {used}/{limit} used · {max(0, limit-used)} left this week"


def admin(interaction):
    return bool(interaction.guild and interaction.user.guild_permissions.administrator)


def owner(interaction):
    return bool(interaction.guild and interaction.user.id == interaction.guild.owner_id)


def valid_url(value):
    try:
        p = urlparse(value.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except ValueError:
        return False


def uptime(seconds):
    d, seconds = divmod(int(seconds), 86400)
    h, seconds = divmod(seconds, 3600)
    m, seconds = divmod(seconds, 60)
    return " ".join(([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + ([f"{m}m"] if m else []) + [f"{seconds}s"])


# -------------------- bot --------------------

intents = discord.Intents.default()
cooldowns = {}  # (guild_id, user_id) -> monotonic timestamp


class AvLinkBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(DispenserPanel())
        try:
            synced = await self.tree.sync()
            print(f"synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"command sync error: {e}")


bot = AvLinkBot(command_prefix="!", intents=intents)


async def update_status():
    if bot.is_ready():
        await bot.change_presence(status=discord.Status.online,
            activity=discord.Game(name=f"giving links to {len(bot.guilds)} servers"))


@bot.event
async def on_ready():
    print(f"avlink is online as {bot.user}")
    await update_status()


@bot.event
async def on_guild_join(guild):
    ensure_settings(guild.id)
    ensure_general(guild.id)
    await update_status()


@bot.event
async def on_guild_remove(guild):
    await update_status()


# -------------------- delivery components --------------------

async def deliver(interaction, gid, cid, method, remove_after):
    cooldown, _, _, limit, enabled = settings_for(gid)
    if not enabled:
        await interaction.response.edit_message(content="the link dispenser is currently disabled by this server's owner.", view=None)
        return

    key = (gid, interaction.user.id)
    if cooldown and key in cooldowns:
        left = cooldown - (time.monotonic() - cooldowns[key])
        if left > 0:
            await interaction.response.edit_message(content=f"please wait {int(left)+1} seconds before getting another link.", view=None)
            return

    used = weekly_used(gid, interaction.user.id)
    if limit and used >= limit:
        await interaction.response.edit_message(content=f"you have reached your weekly link limit ({limit}).", view=None)
        return

    rows = links_for(gid, cid)
    if not rows:
        await interaction.response.edit_message(content="there are no links available in that category.", view=None)
        return

    row = random.choice(rows)
    url = row["url"]
    quota = quota_text(gid, interaction.user.id, after_delivery=True)
    message = f"your avlink:\n{url}\n\n{quota}"

    if method == "dm":
        try:
            await interaction.user.send(message)
        except discord.Forbidden:
            await interaction.response.edit_message(content="i couldn't send you a dm. enable direct messages from this server and try again.", view=None)
            return
        except discord.HTTPException:
            await interaction.response.edit_message(content="discord couldn't deliver your dm right now. please try again.", view=None)
            return
        await interaction.response.edit_message(content="link sent to your dms.", view=None)
    else:
        await interaction.response.edit_message(content=message, view=None)

    weekly_increment(gid, interaction.user.id)
    cooldowns[key] = time.monotonic()
    if remove_after:
        db.execute("DELETE FROM links WHERE id=? AND guild_id=?", (row["id"], gid))
        db.commit()


class DeliverySelect(discord.ui.Select):
    def __init__(self, gid, cid, default, remove_after):
        self.gid, self.cid, self.remove_after = gid, cid, remove_after
        # Keep both options selectable; show the configured default in the placeholder.
        super().__init__(placeholder=f"choose delivery method (default: {default})", options=[
            discord.SelectOption(label="dm", description="send the link to your direct messages", value="dm"),
            discord.SelectOption(label="private", description="show the link only to you in this channel", value="private")
        ])

    async def callback(self, interaction):
        await deliver(interaction, self.gid, self.cid, self.values[0], self.remove_after)


class DeliveryView(discord.ui.View):
    def __init__(self, gid, cid, default, remove_after):
        super().__init__(timeout=60)
        self.add_item(DeliverySelect(gid, cid, default, remove_after))


class CategorySelect(discord.ui.Select):
    def __init__(self, gid, default, remove_after):
        self.gid, self.default, self.remove_after = gid, default, remove_after
        rows = categories_for(gid)
        super().__init__(placeholder="choose a category", options=[
            discord.SelectOption(label=r["name"][:100], value=str(r["id"]), description=f"get a link from {r['name']}"[:100])
            for r in rows[:25]
        ])

    async def callback(self, interaction):
        _, _, default, _, enabled = settings_for(self.gid)
        if not enabled:
            await interaction.response.edit_message(content="the link dispenser is currently disabled by this server's owner.", view=None)
            return
        cid = int(self.values[0])
        cat = db.execute("SELECT name FROM categories WHERE id=? AND guild_id=?", (cid, self.gid)).fetchone()
        if not cat:
            await interaction.response.edit_message(content="that category no longer exists. reopen the dispenser.", view=None)
            return
        if not links_for(self.gid, cid):
            await interaction.response.edit_message(content=f"there are no links in {cat['name']}.", view=None)
            return
        await interaction.response.edit_message(content=f"category: {cat['name']}\nchoose how you want to receive your link.", view=DeliveryView(self.gid, cid, default, self.remove_after))


class CategoryView(discord.ui.View):
    def __init__(self, gid, default, remove_after):
        super().__init__(timeout=60)
        self.add_item(CategorySelect(gid, default, remove_after))


def panel_embed(gid):
    _, _, default, _, _ = settings_for(gid)
    e = discord.Embed(
        title="avlink link dispenser",
        description="select the button below, then choose a category to receive a random link.",
        color=discord.Color.blurple(),
    )
    e.add_field(name="available links", value=str(len(links_for(gid))), inline=True)
    e.add_field(name="default delivery", value=default, inline=True)
    return e


class DispenserPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="get random link", style=discord.ButtonStyle.primary, custom_id="avlink_get_random")
    async def get_random(self, interaction, button):
        if not interaction.guild:
            await interaction.response.send_message("use the dispenser inside a server.", ephemeral=True)
            return
        gid = interaction.guild.id
        cooldown, remove, default, limit, enabled = settings_for(gid)
        if not enabled:
            await interaction.response.send_message("the link dispenser is currently disabled by this server's owner.", ephemeral=True)
            return
        if not links_for(gid):
            await interaction.response.send_message("there are currently no links available.", ephemeral=True)
            return
        key = (gid, interaction.user.id)
        if cooldown and key in cooldowns:
            left = cooldown - (time.monotonic() - cooldowns[key])
            if left > 0:
                await interaction.response.send_message(f"please wait {int(left)+1} seconds before getting another link.", ephemeral=True)
                return
        used = weekly_used(gid, interaction.user.id)
        if limit and used >= limit:
            await interaction.response.send_message(f"you have reached your weekly link limit ({limit}).", ephemeral=True)
            return
        cats = categories_for(gid)
        if len(cats) > 25:
            await interaction.response.send_message("this server has more than 25 categories. remove some before using the dispenser.", ephemeral=True)
            return
        await interaction.response.send_message("choose a category.", view=CategoryView(gid, default, remove), ephemeral=True)

    @discord.ui.button(label="refresh", style=discord.ButtonStyle.success, custom_id="avlink_refresh_panel")
    async def refresh_panel(self, interaction, button):
        if not interaction.guild:
            await interaction.response.send_message("use this button inside a server.", ephemeral=True)
            return
        is_server_owner = interaction.user.id == interaction.guild.owner_id
        has_administrator = interaction.user.guild_permissions.administrator
        if not (is_server_owner and has_administrator):
            await interaction.response.send_message(
                "refreshing the panel requires both server-owner and administrator permissions.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=panel_embed(interaction.guild.id),
            view=DispenserPanel(),
        )


# -------------------- commands --------------------

@bot.tree.command(name="linkadd", description="add a link to the avlink dispenser")
@app_commands.describe(link="the link to add", category="the category for this link")
async def linkadd(interaction: discord.Interaction, link: str, category: str = "general"):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    link = link.strip()
    if not valid_url(link):
        await interaction.response.send_message("please provide a valid http or https link.", ephemeral=True); return
    cat = category_for(interaction.guild.id, category)
    if not cat:
        await interaction.response.send_message(f"category `{category.lower()}` doesn't exist. use /categoryadd first.", ephemeral=True); return
    try:
        db.execute("INSERT INTO links(guild_id,url,category_id) VALUES(?,?,?)", (interaction.guild.id, link, cat["id"])); db.commit()
    except sqlite3.IntegrityError:
        await interaction.response.send_message("that link is already in this server's dispenser.", ephemeral=True); return
    await interaction.response.send_message(f"link added to `{cat['name']}`.", ephemeral=True)


@bot.tree.command(name="masslinkadd", description="add multiple links at once")
@app_commands.describe(links="paste links separated by new lines, spaces, or commas", category="optional category; defaults to general")
async def masslinkadd(interaction: discord.Interaction, links: str, category: str = "general"):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    cat = category_for(interaction.guild.id, category)
    if not cat:
        await interaction.response.send_message(f"category `{category.lower()}` doesn't exist. use /categoryadd first.", ephemeral=True); return
    entries = [x.strip().strip("<>") for x in re.split(r"[\s,]+", links) if x.strip()]
    if not entries:
        await interaction.response.send_message("paste at least one link.", ephemeral=True); return
    if len(entries) > 200:
        await interaction.response.send_message("please add 200 links or fewer per command.", ephemeral=True); return
    added = invalid = duplicates = 0
    for url in entries:
        if not valid_url(url):
            invalid += 1; continue
        try:
            db.execute("INSERT INTO links(guild_id,url,category_id) VALUES(?,?,?)", (interaction.guild.id, url, cat["id"]))
            added += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    db.commit()
    await interaction.response.send_message(f"mass add complete for `{cat['name']}`.\nadded: {added}\nduplicates skipped: {duplicates}\ninvalid entries skipped: {invalid}", ephemeral=True)


@bot.tree.command(name="linkremove", description="remove a link from the avlink dispenser")
@app_commands.describe(link="the exact link to remove")
async def linkremove(interaction: discord.Interaction, link: str):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    cur = db.execute("DELETE FROM links WHERE guild_id=? AND url=?", (interaction.guild.id, link.strip())); db.commit()
    if not cur.rowcount:
        await interaction.response.send_message("that link isn't in this server's dispenser.", ephemeral=True); return
    await interaction.response.send_message("link removed from the dispenser.", ephemeral=True)


@bot.tree.command(name="categoryadd", description="add a category to the avlink dispenser")
@app_commands.describe(category="the name of the new category")
async def categoryadd(interaction: discord.Interaction, category: str):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    category = category.strip().lower()
    if not category or len(category) > 50:
        await interaction.response.send_message("category names must be 1–50 characters.", ephemeral=True); return
    if len(categories_for(interaction.guild.id)) >= 25:
        await interaction.response.send_message("discord allows up to 25 categories in the dispenser dropdown.", ephemeral=True); return
    if category_for(interaction.guild.id, category):
        await interaction.response.send_message("that category already exists.", ephemeral=True); return
    db.execute("INSERT INTO categories(guild_id,name) VALUES(?,?)", (interaction.guild.id, category)); db.commit()
    await interaction.response.send_message(f"category `{category}` added.", ephemeral=True)


class LinkPages(discord.ui.View):
    def __init__(self, gid, rows):
        super().__init__(timeout=120); self.gid=gid; self.rows=rows; self.page=0; self.per_page=10; self.update_buttons()
    def total(self): return max(1, (len(self.rows)+self.per_page-1)//self.per_page)
    def update_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total()-1
    def embed(self):
        part=self.rows[self.page*self.per_page:(self.page+1)*self.per_page]
        e=discord.Embed(title="avlink current links", description="links stored in this server's dispenser.", color=discord.Color.blurple())
        if not part: e.description="there are currently no links."
        for i,row in enumerate(part, start=self.page*self.per_page+1):
            c=db.execute("SELECT name FROM categories WHERE id=? AND guild_id=?", (row["category_id"],self.gid)).fetchone()
            e.add_field(name=f"{i}. {c['name'] if c else 'uncategorized'}", value=row["url"][:1024], inline=False)
        e.set_footer(text=f"page {self.page+1}/{self.total()} · {len(self.rows)} total links")
        return e
    @discord.ui.button(label="previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page=max(0,self.page-1); self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(),view=self)
    @discord.ui.button(label="next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page=min(self.total()-1,self.page+1); self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(),view=self)


@bot.tree.command(name="currentlinks", description="show the current links in the dispenser")
async def currentlinks(interaction: discord.Interaction):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    view=LinkPages(interaction.guild.id, links_for(interaction.guild.id))
    await interaction.response.send_message(embed=view.embed(),view=view,ephemeral=True)


@bot.tree.command(name="panel", description="create the avlink dispenser panel")
@app_commands.describe(channel="the channel where the panel should be posted")
async def panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.", ephemeral=True); return
    _, _, default, _, enabled=settings_for(interaction.guild.id)
    if not enabled:
        await interaction.response.send_message("the dispenser is disabled. the server owner must use /enablepanel first.", ephemeral=True); return
    target=channel or interaction.channel
    e=panel_embed(interaction.guild.id)
    try: await target.send(embed=e,view=DispenserPanel())
    except discord.Forbidden:
        await interaction.response.send_message("i can't post there. check view channel, send messages, and embed links permissions.",ephemeral=True); return
    await interaction.response.send_message(f"avlink panel posted in {target.mention}.",ephemeral=True)


@bot.tree.command(name="disablepanel", description="disable this server's dispenser (server owner only)")
async def disablepanel(interaction: discord.Interaction):
    if not owner(interaction):
        await interaction.response.send_message("only this server's owner can disable the dispenser.",ephemeral=True); return
    ensure_settings(interaction.guild.id)
    db.execute("UPDATE settings SET panel_enabled=0 WHERE guild_id=?",(interaction.guild.id,)); db.commit()
    await interaction.response.send_message("this server's dispenser is disabled. links, categories, and settings are preserved. use /enablepanel to restore it.",ephemeral=True)


@bot.tree.command(name="enablepanel", description="enable this server's dispenser (server owner only)")
async def enablepanel(interaction: discord.Interaction):
    if not owner(interaction):
        await interaction.response.send_message("only this server's owner can enable the dispenser.",ephemeral=True); return
    ensure_settings(interaction.guild.id)
    db.execute("UPDATE settings SET panel_enabled=1 WHERE guild_id=?",(interaction.guild.id,)); db.commit()
    await interaction.response.send_message("this server's dispenser is enabled again.",ephemeral=True)


# -------------------- settings --------------------

def settings_embed(gid):
    cooldown,remove,delivery,limit,enabled=settings_for(gid)
    e=discord.Embed(title="avlink settings",description="manage this server's link dispenser.",color=discord.Color.blurple())
    e.add_field(name="links",value=str(len(links_for(gid))),inline=True)
    e.add_field(name="categories",value=str(len(categories_for(gid))),inline=True)
    e.add_field(name="cooldown",value="disabled" if not cooldown else f"{cooldown} seconds",inline=True)
    e.add_field(name="remove after use",value="enabled" if remove else "disabled",inline=True)
    e.add_field(name="default delivery",value=delivery,inline=True)
    e.add_field(name="weekly limit",value="unlimited" if not limit else f"{limit} links per user",inline=True)
    e.add_field(name="dispenser",value="enabled" if enabled else "disabled",inline=True)
    return e


class NumberModal(discord.ui.Modal):
    def __init__(self,gid,column,title,label,maximum):
        super().__init__(title=title); self.gid=gid; self.column=column; self.maximum=maximum
        self.amount=discord.ui.TextInput(label=label,placeholder=f"0–{maximum}",max_length=5)
        self.add_item(self.amount)
    async def on_submit(self,interaction):
        try:
            value=int(self.amount.value.strip())
            if not 0<=value<=self.maximum: raise ValueError
        except ValueError:
            await interaction.response.send_message(f"enter a whole number between 0 and {self.maximum}.",ephemeral=True); return
        if self.column not in ("cooldown","weekly_limit"):
            await interaction.response.send_message("unsupported setting.",ephemeral=True); return
        db.execute(f"UPDATE settings SET {self.column}=? WHERE guild_id=?",(value,self.gid)); db.commit()
        await interaction.response.send_message(f"setting updated to {value}.",ephemeral=True)


class DeliveryModal(discord.ui.Modal):
    def __init__(self,gid):
        super().__init__(title="set default delivery"); self.gid=gid
        self.delivery=discord.ui.TextInput(label="delivery method",placeholder="dm or private",max_length=7); self.add_item(self.delivery)
    async def on_submit(self,interaction):
        value=self.delivery.value.strip().lower()
        if value not in ("dm","private"):
            await interaction.response.send_message("enter either dm or private.",ephemeral=True); return
        db.execute("UPDATE settings SET default_delivery=? WHERE guild_id=?",(value,self.gid)); db.commit()
        await interaction.response.send_message(f"default delivery set to {value}.",ephemeral=True)


class SettingsSelect(discord.ui.Select):
    def __init__(self,gid):
        self.gid=gid
        super().__init__(placeholder="choose a setting",options=[
            discord.SelectOption(label="remove after use",value="remove",description="toggle used links being removed"),
            discord.SelectOption(label="cooldown",value="cooldown",description="set the user cooldown"),
            discord.SelectOption(label="default delivery",value="delivery",description="set dm or private"),
            discord.SelectOption(label="weekly limit",value="weekly",description="set the per-user weekly limit")])
    async def callback(self,interaction):
        if not admin(interaction):
            await interaction.response.send_message("administrator permissions required.",ephemeral=True); return
        choice=self.values[0]
        if choice=="remove":
            _,current,_,_,_=settings_for(self.gid)
            db.execute("UPDATE settings SET remove_after_use=? WHERE guild_id=?",(0 if current else 1,self.gid)); db.commit()
            await interaction.response.edit_message(embed=settings_embed(self.gid),view=SettingsView(self.gid))
        elif choice=="cooldown":
            await interaction.response.send_modal(NumberModal(self.gid,"cooldown","set cooldown","cooldown in seconds",86400))
        elif choice=="weekly":
            await interaction.response.send_modal(NumberModal(self.gid,"weekly_limit","set weekly limit","links per week (0 = unlimited)",10000))
        else:
            await interaction.response.send_modal(DeliveryModal(self.gid))


class SettingsView(discord.ui.View):
    def __init__(self,gid):
        super().__init__(timeout=180); self.add_item(SettingsSelect(gid))


@bot.tree.command(name="settings", description="open the avlink settings panel")
async def settings(interaction: discord.Interaction):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.",ephemeral=True); return
    await interaction.response.send_message(embed=settings_embed(interaction.guild.id),view=SettingsView(interaction.guild.id),ephemeral=True)


@bot.tree.command(name="setweeklylimit", description="set the weekly link limit for each user")
@app_commands.describe(amount="maximum links each user can receive per week; 0 means unlimited")
async def setweeklylimit(interaction: discord.Interaction, amount: app_commands.Range[int,0,10000]):
    if not admin(interaction):
        await interaction.response.send_message("administrator permissions required.",ephemeral=True); return
    ensure_settings(interaction.guild.id)
    db.execute("UPDATE settings SET weekly_limit=? WHERE guild_id=?",(amount,interaction.guild.id)); db.commit()
    await interaction.response.send_message("weekly link limit disabled." if amount==0 else f"weekly limit set to {amount} links per user.",ephemeral=True)


@bot.tree.command(name="ping", description="check bot latency, uptime, and active servers")
async def ping(interaction: discord.Interaction):
    e=discord.Embed(title="avlink status",color=discord.Color.blurple())
    e.add_field(name="latency",value=f"{round(bot.latency*1000)} ms",inline=True)
    e.add_field(name="uptime",value=uptime(time.monotonic()-START_TIME),inline=True)
    e.add_field(name="active servers",value=str(len(bot.guilds)),inline=True)
    await interaction.response.send_message(embed=e,ephemeral=True)


bot.run(TOKEN)
