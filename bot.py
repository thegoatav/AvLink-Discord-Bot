import os
import random
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

# =========================
# DATABASE
# =========================

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
        remove_after_use INTEGER DEFAULT 0
    )
""")
db.commit()


# =========================
# BOT
# =========================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================
# HELPERS
# =========================

def admin_only(interaction):
    return (
        interaction.guild
        and interaction.user.guild_permissions.administrator
    )


def get_links():
    return db.execute(
        "SELECT id, url FROM links ORDER BY id"
    ).fetchall()


def get_settings(guild_id):
    result = db.execute(
        "SELECT cooldown, remove_after_use FROM settings WHERE guild_id = ?",
        (guild_id,)
    ).fetchone()

    if result:
        return result

    db.execute(
        "INSERT INTO settings (guild_id) VALUES (?)",
        (guild_id,)
    )
    db.commit()

    return (0, 0)


# =========================
# READY
# =========================

@bot.event
async def on_ready():
    print(f"AvLink is online as {bot.user}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Command sync failed: {e}")


# =========================
# /linkadd
# =========================

@bot.tree.command(
    name="linkadd",
    description="Add a link to the AvLink dispenser."
)
@app_commands.describe(link="The link to add.")
async def linkadd(
    interaction: discord.Interaction,
    link: str
):
    if not admin_only(interaction):
        await interaction.response.send_message(
            "❌ Administrator permissions required.",
            ephemeral=True
        )
        return

    if not link.startswith(("http://", "https://")):
        await interaction.response.send_message(
            "❌ That doesn't look like a valid link.",
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
            "⚠️ That link is already in AvLink.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "✅ Link added to the AvLink dispenser.",
        ephemeral=True
    )


# =========================
# /linkremove
# =========================

@bot.tree.command(
    name="linkremove",
    description="Remove a link from the AvLink dispenser."
)
@app_commands.describe(link="The link to remove.")
async def linkremove(
    interaction: discord.Interaction,
    link: str
):
    if not admin_only(interaction):
        await interaction.response.send_message(
            "❌ Administrator permissions required.",
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
            "❌ That link isn't in the dispenser.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "✅ Link removed.",
        ephemeral=True
    )


# =========================
# /currentlinks
# =========================

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
        current = self.links[start:start + self.per_page]

        embed = discord.Embed(
            title="🔗 AvLink — Current Links",
            description="Links currently stored in the dispenser.",
            color=discord.Color.blurple()
        )

        if not current:
            embed.description = "There are currently no links."

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
            text=f"Page {self.page + 1}/{self.total_pages()} • "
                 f"{len(self.links)} total links"
        )

        return embed

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
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
        label="Next",
        emoji="▶️",
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
    description="View the links currently in the dispenser."
)
async def currentlinks(interaction: discord.Interaction):

    links = get_links()
    view = LinkPages(links)

    await interaction.response.send_message(
        embed=view.make_embed(),
        view=view,
        ephemeral=True
    )


# =========================
# DISPENSER PANEL
# =========================

class DispenserPanel(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get Random Link",
        emoji="🎲",
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
                "❌ There are currently no links available.",
                ephemeral=True
            )
            return

        link_id, url = random.choice(links)

        try:
            await interaction.user.send(
                f"🎮 **Your AvLink**\n\n{url}"
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't DM you. Please enable DMs from "
                "server members.",
                ephemeral=True
            )
            return

        # Check whether links should be removed after use.
        if interaction.guild:
            _, remove_after_use = get_settings(
                interaction.guild.id
            )

            if remove_after_use:
                db.execute(
                    "DELETE FROM links WHERE id = ?",
                    (link_id,)
                )
                db.commit()

        await interaction.response.send_message(
            "📩 Sent! Check your DMs.",
            ephemeral=True
        )


# =========================
# /panel
# =========================

@bot.tree.command(
    name="panel",
    description="Create the AvLink dispenser panel."
)
@app_commands.describe(
    channel="The channel where the panel should be posted."
)
async def panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None
):

    if not admin_only(interaction):
        await interaction.response.send_message(
            "❌ Administrator permissions required.",
            ephemeral=True
        )
        return

    target = channel or interaction.channel

    links = get_links()

    embed = discord.Embed(
        title="🎮 AvLink Link Dispenser",
        description=(
            "Welcome to **AvLink**!\n\n"
            "Click the button below to receive a random "
            "available link through DM."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🔗 Available Links",
        value=str(len(links)),
        inline=True
    )

    embed.add_field(
        name="📩 Delivery",
        value="DMs",
        inline=True
    )

    await target.send(
        embed=embed,
        view=DispenserPanel()
    )

    await interaction.response.send_message(
        f"✅ AvLink panel posted in {target.mention}.",
        ephemeral=True
    )


# =========================
# /settings
# =========================

class SettingsPanel(discord.ui.View):

    def __init__(self, guild_id):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(
        label="Remove After Use",
        emoji="🗑️",
        style=discord.ButtonStyle.secondary
    )
    async def remove_toggle(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not admin_only(interaction):
            await interaction.response.send_message(
                "❌ Administrator permissions required.",
                ephemeral=True
            )
            return

        cooldown, current = get_settings(self.guild_id)

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
            f"🗑️ Remove after use is now "
            f"**{'ON' if new_value else 'OFF'}**.",
            ephemeral=True
        )


@bot.tree.command(
    name="settings",
    description="Open the AvLink settings panel."
)
async def settings(interaction: discord.Interaction):

    if not admin_only(interaction):
        await interaction.response.send_message(
            "❌ Administrator permissions required.",
            ephemeral=True
        )
        return

    cooldown, remove = get_settings(
        interaction.guild.id
    )

    links = get_links()

    embed = discord.Embed(
        title="⚙️ AvLink Settings",
        description="Configure your AvLink dispenser.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🔗 Links",
        value=str(len(links)),
        inline=True
    )

    embed.add_field(
        name="⏱️ Cooldown",
        value=f"{cooldown}s",
        inline=True
    )

    embed.add_field(
        name="🗑️ Remove After Use",
        value="ON" if remove else "OFF",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        view=SettingsPanel(interaction.guild.id),
        ephemeral=True
    )


# =========================
# START
# =========================

bot.run(TOKEN)
