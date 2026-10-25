import os
import random
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

START_TIME = time.monotonic()


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect("avlink.db")

db.execute("""
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    cooldown INTEGER DEFAULT 0,
    remove_after_use INTEGER DEFAULT 0,
    default_delivery TEXT DEFAULT 'dm'
)
""")

db.commit()


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# user_id -> last dispenser use
cooldowns = {}


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_links():
    return db.execute(
        "SELECT id, url FROM links ORDER BY id"
    ).fetchall()


def get_settings(guild_id):
    result = db.execute(
        """
        SELECT cooldown, remove_after_use, default_delivery
        FROM settings
        WHERE guild_id = ?
        """,
        (guild_id,)
    ).fetchone()

    if result:
        return result

    db.execute(
        """
        INSERT INTO settings
        (guild_id, cooldown, remove_after_use, default_delivery)
        VALUES (?, 0, 0, 'dm')
        """,
        (guild_id,)
    )

    db.commit()

    return (0, 0, "dm")


def is_admin(interaction: discord.Interaction):
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.administrator
    )


def format_uptime(seconds):
    seconds = int(seconds)

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    parts.append(f"{seconds}s")

    return " ".join(parts)


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    print(f"AvLink is online as {bot.user}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as error:
        print(f"Command sync error: {error}")


# =========================================================
# PERSISTENT VIEWS
# =========================================================

class DispenserPanel(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get random link",
        style=discord.ButtonStyle.primary,
        custom_id="avlink_get_random"
    )
    async def get_random(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        links = get_links()

        if not links:
            await interaction.response.send_message(
                "There are currently no links available.",
                ephemeral=True
            )
            return

        # -----------------------------------------
        # COOLDOWN
        # -----------------------------------------

        if interaction.guild:
            cooldown, remove_after_use, default_delivery = get_settings(
                interaction.guild.id
            )
        else:
            cooldown = 0
            remove_after_use = 0
            default_delivery = "dm"

        now = time.monotonic()

        if cooldown > 0:
            last_use = cooldowns.get(interaction.user.id)

            if last_use:
                remaining = cooldown - (now - last_use)

                if remaining > 0:
                    await interaction.response.send_message(
                        f"Please wait {int(remaining) + 1} seconds before getting another link.",
                        ephemeral=True
                    )
                    return

        # -----------------------------------------
        # CHOOSE DELIVERY METHOD
        # -----------------------------------------

        view = DeliveryView(
            links=links,
            default_delivery=default_delivery,
            remove_after_use=bool(remove_after_use),
            cooldown=cooldown
        )

        await interaction.response.send_message(
            "Choose how you want to receive your link.",
            view=view,
            ephemeral=True
        )


# =========================================================
# DELIVERY SELECTOR
# =========================================================

class DeliverySelect(discord.ui.Select):

    def __init__(
        self,
        links,
        default_delivery,
        remove_after_use,
        cooldown
    ):

        self.links = links
        self.remove_after_use = remove_after_use
        self.cooldown = cooldown

        default_index = 0 if default_delivery == "dm" else 1

        options = [
            discord.SelectOption(
                label="dm",
                description="send the link to your direct messages",
                value="dm",
                default=default_index == 0
            ),
            discord.SelectOption(
                label="private message",
                description="show the link only to you in this channel",
                value="private",
                default=default_index == 1
            )
        ]

        super().__init__(
            placeholder="choose delivery method",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        method = self.values[0]

        link_id, url = random.choice(self.links)

        # -----------------------------------------
        # COOLDOWN STARTS AFTER SUCCESSFUL SELECTION
        # -----------------------------------------

        cooldowns[interaction.user.id] = time.monotonic()

        # -----------------------------------------
        # DM
        # -----------------------------------------

        if method == "dm":

            try:
                await interaction.user.send(
                    f"your avlink:\n{url}"
                )

                if self.remove_after_use:
                    db.execute(
                        "DELETE FROM links WHERE id = ?",
                        (link_id,)
                    )
                    db.commit()

                await interaction.response.edit_message(
                    content="link sent to your dms.",
                    view=None
                )

            except discord.Forbidden:

                await interaction.response.edit_message(
                    content=(
                        "i couldn't send you a dm. "
                        "please enable direct messages from this server."
                    ),
                    view=None
                )

            return

        # -----------------------------------------
        # PRIVATE CHANNEL MESSAGE
        # -----------------------------------------

        if self.remove_after_use:
            db.execute(
                "DELETE FROM links WHERE id = ?",
                (link_id,)
            )
            db.commit()

        await interaction.response.edit_message(
            content=f"your avlink:\n{url}",
            view=None
        )


class DeliveryView(discord.ui.View):

    def __init__(
        self,
        links,
        default_delivery,
        remove_after_use,
        cooldown
    ):

        super().__init__(timeout=60)

        self.add_item(
            DeliverySelect(
                links,
                default_delivery,
                remove_after_use,
                cooldown
            )
        )


# =========================================================
# /linkadd
# =========================================================

@bot.tree.command(
    name="linkadd",
    description="add a link to the avlink dispenser"
)
@app_commands.describe(
    link="the link to add"
)
async def linkadd(
    interaction: discord.Interaction,
    link: str
):

    if not is_admin(interaction):
        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )
        return

    if not link.startswith(("http://", "https://")):
        await interaction.response.send_message(
            "please provide a valid http or https link.",
            ephemeral=True
        )
        return

    try:
        db.execute(
            "INSERT INTO links (url) VALUES (?)",
            (link,)
        )

        db.commit()

    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            "that link is already in the dispenser.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "link added to the avlink dispenser.",
        ephemeral=True
    )


# =========================================================
# /linkremove
# =========================================================

@bot.tree.command(
    name="linkremove",
    description="remove a link from the avlink dispenser"
)
@app_commands.describe(
    link="the link to remove"
)
async def linkremove(
    interaction: discord.Interaction,
    link: str
):

    if not is_admin(interaction):
        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )
        return

    result = db.execute(
        "DELETE FROM links WHERE url = ?",
        (link,)
    )

    db.commit()

    if result.rowcount == 0:
        await interaction.response.send_message(
            "that link isn't in the dispenser.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "link removed from the dispenser.",
        ephemeral=True
    )


# =========================================================
# /currentlinks
# =========================================================

class LinkPages(discord.ui.View):

    def __init__(self, links):
        super().__init__(timeout=120)

        self.links = links
        self.page = 0
        self.per_page = 10

        self.update_buttons()

    def total_pages(self):
        return max(
            1,
            (len(self.links) + self.per_page - 1)
            // self.per_page
        )

    def update_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.total_pages() - 1

    def make_embed(self):

        start = self.page * self.per_page

        current = self.links[
            start:start + self.per_page
        ]

        embed = discord.Embed(
            title="avlink current links",
            description="links currently stored in the dispenser.",
            color=discord.Color.blurple()
        )

        if not current:
            embed.description = "there are currently no links."

        for number, (_, url) in enumerate(
            current,
            start=start + 1
        ):

            embed.add_field(
                name=f"{number}.",
                value=url,
                inline=False
            )

        embed.set_footer(
            text=(
                f"page {self.page + 1}/{self.total_pages()} "
                f"• {len(self.links)} total links"
            )
        )

        return embed

    @discord.ui.button(
        label="previous",
        style=discord.ButtonStyle.secondary
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page -= 1
        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.make_embed(),
            view=self
        )

    @discord.ui.button(
        label="next",
        style=discord.ButtonStyle.secondary
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.page += 1
        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.make_embed(),
            view=self
        )


@bot.tree.command(
    name="currentlinks",
    description="show the current links in the dispenser"
)
async def currentlinks(interaction: discord.Interaction):

    links = get_links()

    view = LinkPages(links)

    await interaction.response.send_message(
        embed=view.make_embed(),
        view=view,
        ephemeral=True
    )


# =========================================================
# /panel
# =========================================================

@bot.tree.command(
    name="panel",
    description="create the avlink dispenser panel"
)
@app_commands.describe(
    channel="the channel where the panel should be posted"
)
async def panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None
):

    if not is_admin(interaction):
        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )
        return

    target = channel or interaction.channel

    links = get_links()

    embed = discord.Embed(
        title="avlink link dispenser",
        description=(
            "select the button below to receive a random "
            "available link."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="available links",
        value=str(len(links)),
        inline=True
    )

    embed.add_field(
        name="delivery",
        value="dm by default",
        inline=True
    )

    await target.send(
        embed=embed,
        view=DispenserPanel()
    )

    await interaction.response.send_message(
        f"avlink panel posted in {target.mention}.",
        ephemeral=True
    )


# =========================================================
# SETTINGS
# =========================================================

class SettingsSelect(discord.ui.Select):

    def __init__(self, guild_id):
        self.guild_id = guild_id

        options = [
            discord.SelectOption(
                label="toggle remove after use",
                description="remove a link after someone receives it",
                value="remove"
            ),
            discord.SelectOption(
                label="set cooldown",
                description="set the time users must wait between links",
                value="cooldown"
            ),
            discord.SelectOption(
                label="set default delivery",
                description="choose the default delivery method",
                value="delivery"
            )
        ]

        super().__init__(
            placeholder="choose a setting",
            options=options
        )

    async def callback(self, interaction: discord.Interaction):

        if not is_admin(interaction):
            await interaction.response.send_message(
                "administrator permissions required.",
                ephemeral=True
            )
            return

        setting = self.values[0]

        if setting == "remove":

            cooldown, current, delivery = get_settings(
                self.guild_id
            )

            new_value = 0 if current else 1

            db.execute(
                """
                UPDATE settings
                SET remove_after_use = ?
                WHERE guild_id = ?
                """,
                (new_value, self.guild_id)
            )

            db.commit()

            await interaction.response.send_message(
                f"remove after use is now "
                f"{'enabled' if new_value else 'disabled'}.",
                ephemeral=True
            )

        elif setting == "cooldown":

            await interaction.response.send_modal(
                CooldownModal(self.guild_id)
            )

        elif setting == "delivery":

            await interaction.response.send_modal(
                DeliveryModal(self.guild_id)
            )


class CooldownModal(discord.ui.Modal):

    def __init__(self, guild_id):
        super().__init__(title="set cooldown")

        self.guild_id = guild_id

        self.seconds = discord.ui.TextInput(
            label="cooldown in seconds",
            placeholder="0, 10, 30, 60...",
            required=True,
            max_length=5
        )

        self.add_item(self.seconds)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        try:
            value = int(self.seconds.value)

            if value < 0 or value > 86400:
                raise ValueError

        except ValueError:
            await interaction.response.send_message(
                "enter a number between 0 and 86400.",
                ephemeral=True
            )
            return

        db.execute(
            """
            UPDATE settings
            SET cooldown = ?
            WHERE guild_id = ?
            """,
            (value, self.guild_id)
        )

        db.commit()

        await interaction.response.send_message(
            f"cooldown set to {value} seconds.",
            ephemeral=True
        )


class DeliveryModal(discord.ui.Modal):

    def __init__(self, guild_id):
        super().__init__(title="set default delivery")

        self.guild_id = guild_id

        self.delivery = discord.ui.TextInput(
            label="delivery method",
            placeholder="dm or private",
            required=True,
            max_length=7
        )

        self.add_item(self.delivery)

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        value = self.delivery.value.lower().strip()

        if value not in ("dm", "private"):
            await interaction.response.send_message(
                "enter either dm or private.",
                ephemeral=True
            )
            return

        db.execute(
            """
            UPDATE settings
            SET default_delivery = ?
            WHERE guild_id = ?
            """,
            (value, self.guild_id)
        )

        db.commit()

        await interaction.response.send_message(
            f"default delivery set to {value}.",
            ephemeral=True
        )


@bot.tree.command(
    name="settings",
    description="open the avlink settings panel"
)
async def settings(interaction: discord.Interaction):

    if not is_admin(interaction):
        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )
        return

    cooldown, remove, delivery = get_settings(
        interaction.guild.id
    )

    embed = discord.Embed(
        title="avlink settings",
        description="manage your avlink dispenser settings.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="total links",
        value=str(len(get_links())),
        inline=True
    )

    embed.add_field(
        name="cooldown",
        value=f"{cooldown} seconds",
        inline=True
    )

    embed.add_field(
        name="remove after use",
        value="enabled" if remove else "disabled",
        inline=True
    )

    embed.add_field(
        name="default delivery",
        value=delivery,
        inline=True
    )

    view = discord.ui.View(timeout=120)

    view.add_item(
        SettingsSelect(interaction.guild.id)
    )

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )


# =========================================================
# /ping
# =========================================================

@bot.tree.command(
    name="ping",
    description="check avlink latency, uptime, and active servers"
)
async def ping(interaction: discord.Interaction):

    latency = round(bot.latency * 1000)

    uptime = format_uptime(
        time.monotonic() - START_TIME
    )

    servers = len(bot.guilds)

    embed = discord.Embed(
        title="avlink status",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="latency",
        value=f"{latency} ms",
        inline=True
    )

    embed.add_field(
        name="uptime",
        value=uptime,
        inline=True
    )

    embed.add_field(
        name="active servers",
        value=str(servers),
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# STARTUP
# =========================================================

async def setup_hook():
    bot.add_view(DispenserPanel())

bot.setup_hook = setup_hook

bot.run(TOKEN)
