import os
import random
import sqlite3
import time
from datetime import datetime, timezone, timedelta

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
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(guild_id, name)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    UNIQUE(guild_id, url)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    cooldown INTEGER DEFAULT 0,
    remove_after_use INTEGER DEFAULT 0,
    default_delivery TEXT DEFAULT 'dm',
    weekly_limit INTEGER DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS weekly_usage (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    week TEXT NOT NULL,
    count INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id, week)
)
""")

db.commit()


# =========================================================
# DATABASE MIGRATION
# =========================================================

def migrate_database():
    settings_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(settings)"
        ).fetchall()
    }

    if "cooldown" not in settings_columns:
        db.execute(
            "ALTER TABLE settings ADD COLUMN cooldown INTEGER DEFAULT 0"
        )

    if "remove_after_use" not in settings_columns:
        db.execute(
            "ALTER TABLE settings ADD COLUMN remove_after_use INTEGER DEFAULT 0"
        )

    if "default_delivery" not in settings_columns:
        db.execute(
            "ALTER TABLE settings ADD COLUMN default_delivery TEXT DEFAULT 'dm'"
        )

    if "weekly_limit" not in settings_columns:
        db.execute(
            "ALTER TABLE settings ADD COLUMN weekly_limit INTEGER DEFAULT 0"
        )

    link_columns = {
        row["name"]
        for row in db.execute(
            "PRAGMA table_info(links)"
        ).fetchall()
    }

    if "guild_id" not in link_columns:
        db.execute(
            "ALTER TABLE links ADD COLUMN guild_id INTEGER DEFAULT 0"
        )

    if "category_id" not in link_columns:
        db.execute(
            "ALTER TABLE links ADD COLUMN category_id INTEGER"
        )

    legacy_category = db.execute(
        """
        SELECT id
        FROM categories
        WHERE guild_id = 0 AND name = 'general'
        """
    ).fetchone()

    if not legacy_category:
        db.execute(
            """
            INSERT INTO categories (guild_id, name)
            VALUES (0, 'general')
            """
        )

        legacy_category_id = db.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    else:
        legacy_category_id = legacy_category["id"]

    db.execute(
        """
        UPDATE links
        SET category_id = ?
        WHERE category_id IS NULL
        """,
        (legacy_category_id,)
    )

    db.commit()


migrate_database()


# =========================================================
# DATABASE HELPERS
# =========================================================

def ensure_general_category(guild_id):
    row = db.execute(
        """
        SELECT id
        FROM categories
        WHERE guild_id = ? AND name = 'general'
        """,
        (guild_id,)
    ).fetchone()

    if row:
        return row["id"]

    db.execute(
        """
        INSERT INTO categories (guild_id, name)
        VALUES (?, 'general')
        """,
        (guild_id,)
    )

    db.commit()

    return db.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]


def get_categories(guild_id):
    ensure_general_category(guild_id)

    return db.execute(
        """
        SELECT id, name
        FROM categories
        WHERE guild_id = ?
        ORDER BY name
        """,
        (guild_id,)
    ).fetchall()


def get_category(guild_id, name):
    return db.execute(
        """
        SELECT id, name
        FROM categories
        WHERE guild_id = ? AND name = ?
        """,
        (guild_id, name.lower().strip())
    ).fetchone()


def get_links(guild_id, category_id=None):

    if category_id is None:
        return db.execute(
            """
            SELECT id, url, category_id
            FROM links
            WHERE guild_id = ?
            ORDER BY id
            """,
            (guild_id,)
        ).fetchall()

    return db.execute(
        """
        SELECT id, url, category_id
        FROM links
        WHERE guild_id = ? AND category_id = ?
        ORDER BY id
        """,
        (guild_id, category_id)
    ).fetchall()


def get_settings(guild_id):

    result = db.execute(
        """
        SELECT
            cooldown,
            remove_after_use,
            default_delivery,
            weekly_limit
        FROM settings
        WHERE guild_id = ?
        """,
        (guild_id,)
    ).fetchone()

    if result:
        return (
            result["cooldown"],
            result["remove_after_use"],
            result["default_delivery"],
            result["weekly_limit"]
        )

    db.execute(
        """
        INSERT INTO settings
        (
            guild_id,
            cooldown,
            remove_after_use,
            default_delivery,
            weekly_limit
        )
        VALUES (?, 0, 0, 'dm', 0)
        """,
        (guild_id,)
    )

    db.commit()

    return 0, 0, "dm", 0


def is_admin(interaction):
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.administrator
    )


# =========================================================
# WEEKLY LIMIT HELPERS
# =========================================================

def current_week():
    now = datetime.now(timezone.utc)

    monday = now - timedelta(
        days=now.weekday()
    )

    return monday.strftime("%Y-%m-%d")


def get_weekly_usage(guild_id, user_id):

    row = db.execute(
        """
        SELECT count
        FROM weekly_usage
        WHERE guild_id = ?
        AND user_id = ?
        AND week = ?
        """,
        (
            guild_id,
            user_id,
            current_week()
        )
    ).fetchone()

    return row["count"] if row else 0


def increment_weekly_usage(guild_id, user_id):

    week = current_week()

    db.execute(
        """
        INSERT INTO weekly_usage
        (
            guild_id,
            user_id,
            week,
            count
        )
        VALUES (?, ?, ?, 1)

        ON CONFLICT(guild_id, user_id, week)
        DO UPDATE SET count = count + 1
        """,
        (
            guild_id,
            user_id,
            week
        )
    )

    db.commit()


# =========================================================
# GENERAL HELPERS
# =========================================================

def format_uptime(seconds):

    seconds = int(seconds)

    days, seconds = divmod(
        seconds,
        86400
    )

    hours, seconds = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        seconds,
        60
    )

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    parts.append(f"{seconds}s")

    return " ".join(parts)


async def update_bot_status():

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            name=f"giving links to {len(bot.guilds)} servers"
        )
    )


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()

user_cooldowns = {}


class AvLinkBot(commands.Bot):

    async def setup_hook(self):

        # Makes the main dispenser button survive restarts.
        self.add_view(
            DispenserPanel()
        )

        try:
            synced = await self.tree.sync()

            print(
                f"synced {len(synced)} slash commands."
            )

        except Exception as error:

            print(
                f"command sync error: {error}"
            )


bot = AvLinkBot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def on_ready():

    print(
        f"avlink is online as {bot.user}"
    )

    await update_bot_status()


@bot.event
async def on_guild_join(guild):

    ensure_general_category(
        guild.id
    )

    await update_bot_status()


@bot.event
async def on_guild_remove(guild):

    await update_bot_status()


# =========================================================
# DELIVERY SELECTOR
# =========================================================

class DeliverySelect(discord.ui.Select):

    def __init__(
        self,
        guild_id,
        category_id,
        default_delivery,
        remove_after_use
    ):

        self.guild_id = guild_id
        self.category_id = category_id
        self.remove_after_use = remove_after_use

        options = [
            discord.SelectOption(
                label="dm",
                description="send the link to your direct messages",
                value="dm",
                default=default_delivery == "dm"
            ),
            discord.SelectOption(
                label="private",
                description="show the link only to you in this channel",
                value="private",
                default=default_delivery == "private"
            )
        ]

        super().__init__(
            placeholder="choose delivery method",
            options=options
        )

    async def callback(self, interaction):

        links = get_links(
            self.guild_id,
            self.category_id
        )

        if not links:

            await interaction.response.edit_message(
                content="there are no links in this category.",
                view=None
            )

            return

        _, _, _, weekly_limit = get_settings(
            self.guild_id
        )

        # Weekly limit check
        if weekly_limit > 0:

            used = get_weekly_usage(
                self.guild_id,
                interaction.user.id
            )

            if used >= weekly_limit:

                await interaction.response.edit_message(
                    content=(
                        "you have reached your weekly link limit. "
                        f"your limit is {weekly_limit} links."
                    ),
                    view=None
                )

                return

        method = self.values[0]

        link_id, url, _ = random.choice(
            links
        )

        # -----------------------------------------
        # DM
        # -----------------------------------------

        if method == "dm":

            try:

                await interaction.user.send(
                    f"your avlink:\n{url}"
                )

            except discord.Forbidden:

                await interaction.response.edit_message(
                    content=(
                        "i couldn't send you a dm. "
                        "please enable direct messages "
                        "from this server."
                    ),
                    view=None
                )

                return

        # -----------------------------------------
        # PRIVATE
        # -----------------------------------------

        else:

            await interaction.response.edit_message(
                content=f"your avlink:\n{url}",
                view=None
            )

        # Successful delivery
        user_cooldowns[
            interaction.user.id
        ] = time.monotonic()

        increment_weekly_usage(
            self.guild_id,
            interaction.user.id
        )

        # Remove link if enabled
        if self.remove_after_use:

            db.execute(
                """
                DELETE FROM links
                WHERE id = ?
                """,
                (link_id,)
            )

            db.commit()

        if method == "dm":

            await interaction.response.edit_message(
                content="link sent to your dms.",
                view=None
            )


class DeliveryView(discord.ui.View):

    def __init__(
        self,
        guild_id,
        category_id,
        default_delivery,
        remove_after_use
    ):

        super().__init__(
            timeout=60
        )

        self.add_item(
            DeliverySelect(
                guild_id,
                category_id,
                default_delivery,
                remove_after_use
            )
        )


# =========================================================
# CATEGORY SELECTOR
# =========================================================

class CategorySelect(discord.ui.Select):

    def __init__(
        self,
        guild_id,
        default_delivery,
        remove_after_use
    ):

        self.guild_id = guild_id
        self.default_delivery = default_delivery
        self.remove_after_use = remove_after_use

        categories = get_categories(
            guild_id
        )

        options = [
            discord.SelectOption(
                label=row["name"][:100],
                value=str(row["id"]),
                description=(
                    f"get a link from "
                    f"{row['name'][:90]}"
                )
            )
            for row in categories[:25]
        ]

        super().__init__(
            placeholder="choose a category",
            options=options
        )

    async def callback(self, interaction):

        category_id = int(
            self.values[0]
        )

        category = db.execute(
            """
            SELECT name
            FROM categories
            WHERE id = ?
            AND guild_id = ?
            """,
            (
                category_id,
                self.guild_id
            )
        ).fetchone()

        if not category:

            await interaction.response.edit_message(
                content="that category no longer exists.",
                view=None
            )

            return

        links = get_links(
            self.guild_id,
            category_id
        )

        if not links:

            await interaction.response.edit_message(
                content=(
                    f"there are no links in "
                    f"{category['name']}."
                ),
                view=None
            )

            return

        await interaction.response.edit_message(
            content=(
                f"category: {category['name']}\n"
                "choose how you want to receive your link."
            ),
            view=DeliveryView(
                guild_id=self.guild_id,
                category_id=category_id,
                default_delivery=self.default_delivery,
                remove_after_use=self.remove_after_use
            )
        )


class CategoryView(discord.ui.View):

    def __init__(
        self,
        guild_id,
        default_delivery,
        remove_after_use
    ):

        super().__init__(
            timeout=60
        )

        self.add_item(
            CategorySelect(
                guild_id,
                default_delivery,
                remove_after_use
            )
        )


# =========================================================
# MAIN DISPENSER PANEL
# =========================================================

class DispenserPanel(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="get random link",
        style=discord.ButtonStyle.primary,
        custom_id="avlink_get_random"
    )
    async def get_random(
        self,
        interaction,
        button
    ):

        guild_id = interaction.guild.id

        categories = get_categories(
            guild_id
        )

        links = get_links(
            guild_id
        )

        if not links:

            await interaction.response.send_message(
                "there are currently no links available.",
                ephemeral=True
            )

            return

        cooldown, remove_after_use, default_delivery, weekly_limit = (
            get_settings(guild_id)
        )

        # -----------------------------------------
        # COOLDOWN
        # -----------------------------------------

        if cooldown > 0:

            last_use = user_cooldowns.get(
                interaction.user.id
            )

            if last_use is not None:

                elapsed = (
                    time.monotonic()
                    - last_use
                )

                remaining = (
                    cooldown
                    - elapsed
                )

                if remaining > 0:

                    await interaction.response.send_message(
                        (
                            f"please wait "
                            f"{int(remaining) + 1} seconds "
                            "before getting another link."
                        ),
                        ephemeral=True
                    )

                    return

        # -----------------------------------------
        # WEEKLY LIMIT
        # -----------------------------------------

        if weekly_limit > 0:

            used = get_weekly_usage(
                guild_id,
                interaction.user.id
            )

            if used >= weekly_limit:

                await interaction.response.send_message(
                    (
                        "you have reached your weekly "
                        "link limit. "
                        f"your limit is {weekly_limit} links."
                    ),
                    ephemeral=True
                )

                return

        # Discord allows a maximum of 25 select options.
        if len(categories) > 25:

            await interaction.response.send_message(
                (
                    "this server has more than 25 categories. "
                    "please remove some categories before "
                    "using the dispenser."
                ),
                ephemeral=True
            )

            return

        await interaction.response.send_message(
            "choose a category.",
            view=CategoryView(
                guild_id,
                default_delivery,
                bool(remove_after_use)
            ),
            ephemeral=True
        )


# =========================================================
# /linkadd
# =========================================================

@bot.tree.command(
    name="linkadd",
    description="add a link to the avlink dispenser"
)
@app_commands.describe(
    link="the link to add",
    category="the category for this link"
)
async def linkadd(
    interaction,
    link: str,
    category: str = "general"
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    if not link.startswith(
        ("http://", "https://")
    ):

        await interaction.response.send_message(
            "please provide a valid http or https link.",
            ephemeral=True
        )

        return

    category = category.lower().strip()

    if len(category) > 50:

        await interaction.response.send_message(
            "category names must be 50 characters or fewer.",
            ephemeral=True
        )

        return

    category_row = get_category(
        interaction.guild.id,
        category
    )

    if not category_row:

        await interaction.response.send_message(
            (
                f"category `{category}` doesn't exist. "
                "use /categoryadd first."
            ),
            ephemeral=True
        )

        return

    try:

        db.execute(
            """
            INSERT INTO links
            (
                guild_id,
                url,
                category_id
            )
            VALUES (?, ?, ?)
            """,
            (
                interaction.guild.id,
                link,
                category_row["id"]
            )
        )

        db.commit()

    except sqlite3.IntegrityError:

        await interaction.response.send_message(
            "that link is already in the dispenser.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        f"link added to `{category}`.",
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
    interaction,
    link: str
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    result = db.execute(
        """
        DELETE FROM links
        WHERE guild_id = ?
        AND url = ?
        """,
        (
            interaction.guild.id,
            link
        )
    )

    db.commit()

    if result.rowcount == 0:

        await interaction.response.send_message(
            "that link isn't in this server's dispenser.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "link removed from the dispenser.",
        ephemeral=True
    )


# =========================================================
# /categoryadd
# =========================================================

@bot.tree.command(
    name="categoryadd",
    description="add a category to the avlink dispenser"
)
@app_commands.describe(
    category="the name of the new category"
)
async def categoryadd(
    interaction,
    category: str
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    category = category.lower().strip()

    if not category:

        await interaction.response.send_message(
            "please enter a category name.",
            ephemeral=True
        )

        return

    if len(category) > 50:

        await interaction.response.send_message(
            "category names must be 50 characters or fewer.",
            ephemeral=True
        )

        return

    categories = get_categories(
        interaction.guild.id
    )

    if len(categories) >= 25:

        await interaction.response.send_message(
            "you can have a maximum of 25 categories.",
            ephemeral=True
        )

        return

    if get_category(
        interaction.guild.id,
        category
    ):

        await interaction.response.send_message(
            "that category already exists.",
            ephemeral=True
        )

        return

    db.execute(
        """
        INSERT INTO categories
        (
            guild_id,
            name
        )
        VALUES (?, ?)
        """,
        (
            interaction.guild.id,
            category
        )
    )

    db.commit()

    await interaction.response.send_message(
        f"category `{category}` added.",
        ephemeral=True
    )


# =========================================================
# /currentlinks
# =========================================================

class LinkPages(discord.ui.View):

    def __init__(
        self,
        guild_id,
        links
    ):

        super().__init__(
            timeout=120
        )

        self.guild_id = guild_id
        self.links = links
        self.page = 0
        self.per_page = 10

        self.update_buttons()

    def total_pages(self):

        return max(
            1,
            (
                len(self.links)
                + self.per_page
                - 1
            )
            // self.per_page
        )

    def update_buttons(self):

        self.previous.disabled = (
            self.page <= 0
        )

        self.next.disabled = (
            self.page >= self.total_pages() - 1
        )

    def make_embed(self):

        start = (
            self.page
            * self.per_page
        )

        current = self.links[
            start:start + self.per_page
        ]

        embed = discord.Embed(
            title="avlink current links",
            description=(
                "links currently stored "
                "in the dispenser."
            ),
            color=discord.Color.blurple()
        )

        if not current:

            embed.description = (
                "there are currently no links."
            )

        for number, row in enumerate(
            current,
            start=start + 1
        ):

            category = db.execute(
                """
                SELECT name
                FROM categories
                WHERE id = ?
                """,
                (row["category_id"],)
            ).fetchone()

            category_name = (
                category["name"]
                if category
                else "general"
            )

            embed.add_field(
                name=(
                    f"{number}. "
                    f"{category_name}"
                ),
                value=row["url"],
                inline=False
            )

        embed.set_footer(
            text=(
                f"page {self.page + 1}"
                f"/{self.total_pages()} "
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
        interaction,
        button
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
        interaction,
        button
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
async def currentlinks(
    interaction
):

    links = get_links(
        interaction.guild.id
    )

    view = LinkPages(
        interaction.guild.id,
        links
    )

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
    interaction,
    channel: discord.TextChannel = None
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    target = (
        channel
        or interaction.channel
    )

    ensure_general_category(
        interaction.guild.id
    )

    links = get_links(
        interaction.guild.id
    )

    embed = discord.Embed(
        title="avlink link dispenser",
        description=(
            "choose a category to get "
            "a random link."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="available links",
        value=str(len(links)),
        inline=True
    )

    embed.add_field(
        name="default delivery",
        value="dm",
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

def make_settings_embed(guild_id):

    cooldown, remove, delivery, weekly_limit = (
        get_settings(guild_id)
    )

    embed = discord.Embed(
        title="avlink settings",
        description=(
            "manage your avlink dispenser."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="links",
        value=str(
            len(get_links(guild_id))
        ),
        inline=True
    )

    embed.add_field(
        name="categories",
        value=str(
            len(get_categories(guild_id))
        ),
        inline=True
    )

    embed.add_field(
        name="cooldown",
        value=(
            "disabled"
            if cooldown == 0
            else f"{cooldown} seconds"
        ),
        inline=True
    )

    embed.add_field(
        name="remove after use",
        value=(
            "enabled"
            if remove
            else "disabled"
        ),
        inline=True
    )

    embed.add_field(
        name="default delivery",
        value=delivery,
        inline=True
    )

    embed.add_field(
        name="weekly limit",
        value=(
            "unlimited"
            if weekly_limit == 0
            else f"{weekly_limit} links"
        ),
        inline=True
    )

    return embed


class SettingsSelect(discord.ui.Select):

    def __init__(
        self,
        guild_id
    ):

        self.guild_id = guild_id

        options = [
            discord.SelectOption(
                label="remove after use",
                description=(
                    "toggle used links "
                    "being removed"
                ),
                value="remove"
            ),
            discord.SelectOption(
                label="cooldown",
                description=(
                    "set the user cooldown"
                ),
                value="cooldown"
            ),
            discord.SelectOption(
                label="default delivery",
                description=(
                    "set the default "
                    "delivery method"
                ),
                value="delivery"
            ),
            discord.SelectOption(
                label="weekly limit",
                description=(
                    "set the weekly "
                    "link limit"
                ),
                value="weekly"
            )
        ]

        super().__init__(
            placeholder="choose a setting",
            options=options
        )

    async def callback(
        self,
        interaction
    ):

        if not is_admin(interaction):

            await interaction.response.send_message(
                "administrator permissions required.",
                ephemeral=True
            )

            return

        setting = self.values[0]

        if setting == "remove":

            cooldown, current, delivery, weekly = (
                get_settings(
                    self.guild_id
                )
            )

            new_value = (
                0
                if current
                else 1
            )

            db.execute(
                """
                UPDATE settings
                SET remove_after_use = ?
                WHERE guild_id = ?
                """,
                (
                    new_value,
                    self.guild_id
                )
            )

            db.commit()

            await interaction.response.edit_message(
                embed=make_settings_embed(
                    self.guild_id
                ),
                view=SettingsView(
                    self.guild_id
                )
            )

        elif setting == "cooldown":

            await interaction.response.send_modal(
                CooldownModal(
                    self.guild_id
                )
            )

        elif setting == "delivery":

            await interaction.response.send_modal(
                DeliveryModal(
                    self.guild_id
                )
            )

        elif setting == "weekly":

            await interaction.response.send_modal(
                WeeklyLimitModal(
                    self.guild_id
                )
            )


class SettingsView(discord.ui.View):

    def __init__(
        self,
        guild_id
    ):

        super().__init__(
            timeout=180
        )

        self.add_item(
            SettingsSelect(
                guild_id
            )
        )


class CooldownModal(discord.ui.Modal):

    def __init__(
        self,
        guild_id
    ):

        super().__init__(
            title="set cooldown"
        )

        self.guild_id = guild_id

        self.seconds = discord.ui.TextInput(
            label="cooldown in seconds",
            placeholder="0, 10, 30, 60...",
            required=True,
            max_length=5
        )

        self.add_item(
            self.seconds
        )

    async def on_submit(
        self,
        interaction
    ):

        try:

            value = int(
                self.seconds.value
            )

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
            (
                value,
                self.guild_id
            )
        )

        db.commit()

        await interaction.response.send_message(
            f"cooldown set to {value} seconds.",
            ephemeral=True
        )


class DeliveryModal(discord.ui.Modal):

    def __init__(
        self,
        guild_id
    ):

        super().__init__(
            title="set default delivery"
        )

        self.guild_id = guild_id

        self.delivery = discord.ui.TextInput(
            label="delivery method",
            placeholder="dm or private",
            required=True,
            max_length=7
        )

        self.add_item(
            self.delivery
        )

    async def on_submit(
        self,
        interaction
    ):

        value = (
            self.delivery.value
            .lower()
            .strip()
        )

        if value not in (
            "dm",
            "private"
        ):

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
            (
                value,
                self.guild_id
            )
        )

        db.commit()

        await interaction.response.send_message(
            f"default delivery set to {value}.",
            ephemeral=True
        )


class WeeklyLimitModal(discord.ui.Modal):

    def __init__(
        self,
        guild_id
    ):

        super().__init__(
            title="set weekly limit"
        )

        self.guild_id = guild_id

        self.limit = discord.ui.TextInput(
            label="links per week",
            placeholder="0 = unlimited",
            required=True,
            max_length=5
        )

        self.add_item(
            self.limit
        )

    async def on_submit(
        self,
        interaction
    ):

        try:

            value = int(
                self.limit.value
            )

            if value < 0 or value > 10000:
                raise ValueError

        except ValueError:

            await interaction.response.send_message(
                "enter a number between 0 and 10000.",
                ephemeral=True
            )

            return

        db.execute(
            """
            UPDATE settings
            SET weekly_limit = ?
            WHERE guild_id = ?
            """,
            (
                value,
                self.guild_id
            )
        )

        db.commit()

        if value == 0:

            message = (
                "weekly link limit disabled."
            )

        else:

            message = (
                f"weekly limit set to "
                f"{value} links per user."
            )

        await interaction.response.send_message(
            message,
            ephemeral=True
        )


# =========================================================
# /settings
# =========================================================

@bot.tree.command(
    name="settings",
    description="open the avlink settings panel"
)
async def settings(
    interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        embed=make_settings_embed(
            interaction.guild.id
        ),
        view=SettingsView(
            interaction.guild.id
        ),
        ephemeral=True
    )


# =========================================================
# /setweeklylimit
# =========================================================

@bot.tree.command(
    name="setweeklylimit",
    description="set the weekly link limit for each user"
)
@app_commands.describe(
    amount=(
        "maximum links each user can receive per week, "
        "0 for unlimited"
    )
)
async def setweeklylimit(
    interaction,
    amount: int
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "administrator permissions required.",
            ephemeral=True
        )

        return

    if amount < 0 or amount > 10000:

        await interaction.response.send_message(
            "the amount must be between 0 and 10000.",
            ephemeral=True
        )

        return

    db.execute(
        """
        UPDATE settings
        SET weekly_limit = ?
        WHERE guild_id = ?
        """,
        (
            amount,
            interaction.guild.id
        )
    )

    db.commit()

    if amount == 0:

        message = (
            "weekly link limit disabled."
        )

    else:

        message = (
            f"weekly link limit set to "
            f"{amount} links per user."
        )

    await interaction.response.send_message(
        message,
        ephemeral=True
    )


# =========================================================
# /ping
# =========================================================

@bot.tree.command(
    name="ping",
    description="check bot latency, uptime, and active servers"
)
async def ping(
    interaction
):

    latency = round(
        bot.latency * 1000
    )

    uptime = format_uptime(
        time.monotonic()
        - START_TIME
    )

    server_count = len(
        bot.guilds
    )

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
        value=str(server_count),
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# START
# =========================================================

bot.run(TOKEN)
