"""Everything related to live tracking is in this cog. The poll_quakes function is the main logic"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import logging
import os
import time

from discord.ext import tasks, commands
import pandas as pd
import discord
import aiohttp

from cogs.earthquake_sources import (
    EarthquakeEvent,
    build_earthquake_sources,
    fetch_events_from_sources,
)
from namazu_utils import (
    DEFAULT_EARTHQUAKE_SOURCES,
    DEFAULT_MAP_LATITUDE,
    DEFAULT_MAP_LONGITUDE,
    DISCORD_FILE_LIMIT_BYTES,
    MAP_STYLE_OPTIONS,
    NUMBER_REACTIONS,
    US_STATE_ABBREV_TO_NAME,
    add_user_country_alert_to_sqlite,
    add_user_state_alert_to_sqlite,
    build_earthquake_csv_parts,
    build_mention_chunks,
    colorize,
    create_embed_quake_alert,
    earthquake_passes_guild_magnitude_filter,
    fetch_earthquake_export_data,
    format_bytes,
    get_default_guild_prefs,
    get_default_user_pref,
    get_eq_db,
    get_eq_notify_db,
    get_guild_prefs,
    get_map_style_option,
    get_user_prefs,
    infer_country_names_from_place,
    infer_us_state_codes_from_place,
    init_sqlite,
    load_eq_db_to_df,
    logger,
    migrate_pickle_data_if_needed,
    normalize_country_name,
    normalize_state_code,
    plot_daily_earthquakes,
    plot_to_img_with_plotly,
    remove_user_country_alert_from_sqlite,
    remove_user_state_alert_from_sqlite,
    sanitize_filename,
    save_eq_db_to_sqlite,
    save_eq_notify_db_to_sqlite,
    save_guild_prefs_to_sqlite,
    upsert_user_magnitude_pref_to_sqlite,
)


class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        init_sqlite()
        migrate_pickle_data_if_needed()
        self.event_sources = build_earthquake_sources(DEFAULT_EARTHQUAKE_SOURCES)
        self.guild_prefs = get_guild_prefs()
        self.eq_notify_db = get_eq_notify_db()
        self.user_prefs = get_user_prefs()
        self.eq_db = get_eq_db()
        logger.info(
            "Configured earthquake sources: %s",
            ", ".join(source.source for source in self.event_sources),
        )
        self.client.loop.create_task(self._initialize())

    def _ensure_guild_pref(self, guild_id: str):
        guild_pref = self.guild_prefs.setdefault(guild_id, get_default_guild_prefs())
        default_pref = get_default_guild_prefs()
        for pref_key, pref_value in default_pref.items():
            guild_pref.setdefault(pref_key, pref_value)

    def _is_enabled_guild(self, guild: discord.Guild):
        if not getattr(self.client, "dev_mode", False):
            return True
        return guild.id == getattr(self.client, "dev_guild_id", 0)

    def _iter_enabled_guilds(self):
        if not getattr(self.client, "dev_mode", False):
            return list(self.client.guilds)

        dev_guild_id = getattr(self.client, "dev_guild_id", 0)
        dev_guild = self.client.get_guild(dev_guild_id)
        if dev_guild is None:
            logging.warning(
                "Dev mode is enabled, but guild %s is not available to this bot.",
                dev_guild_id,
            )
            return []

        return [dev_guild]

    def _find_text_channel_by_name(self, guild: discord.Guild, channel_name: str):
        normalized_name = str(channel_name).strip().lstrip("#").casefold()
        if not normalized_name:
            return None

        return discord.utils.find(
            lambda channel: channel.name.casefold() == normalized_name,
            guild.text_channels,
        )

    def _get_update_channel(self, guild: discord.Guild):
        guild_id = str(guild.id)
        self._ensure_guild_pref(guild_id)
        configured_channel_id = self.guild_prefs[guild_id].get("UpdateChannelId", 0)

        if configured_channel_id:
            channel = guild.get_channel(int(configured_channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel

            logging.warning(
                "Configured update channel %s is unavailable in guild %s. Clearing saved channel.",
                configured_channel_id,
                guild.name,
            )
            self.guild_prefs[guild_id]["UpdateChannelId"] = 0
            save_guild_prefs_to_sqlite(self.guild_prefs)

        legacy_channel = self._find_text_channel_by_name(guild, "quake-updates")
        if legacy_channel is not None:
            self.guild_prefs[guild_id]["UpdateChannelId"] = legacy_channel.id
            save_guild_prefs_to_sqlite(self.guild_prefs)

        return legacy_channel

    def _ensure_user_pref(self, guild_id: str, user_id: str):
        guild_user_prefs = self.user_prefs.setdefault(guild_id, {})
        if not guild_user_prefs.get(user_id):
            guild_user_prefs[user_id] = get_default_user_pref()
        return guild_user_prefs[user_id]

    async def _send_map_style_preview_batches(
        self,
        ctx: commands.Context,
        embeds: list[discord.Embed],
        files: list[discord.File],
        batch_size: int = 3,
    ):
        """Send map style previews in smaller batches so Discord reliably renders each image."""
        for batch_start in range(0, len(embeds), batch_size):
            content = "Map style previews:" if batch_start == 0 else None
            await ctx.send(
                content=content,
                embeds=embeds[batch_start:batch_start + batch_size],
                files=files[batch_start:batch_start + batch_size],
            )

    def _set_user_magnitude_alert(self, guild_id: str, user_id: str, threshold: float):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["MagnitudeMentionEnabled"] = True
        user_pref["MagnitudeThreshold"] = threshold
        upsert_user_magnitude_pref_to_sqlite(guild_id, user_id, True, threshold)

    def _disable_user_magnitude_alert(self, guild_id: str, user_id: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["MagnitudeMentionEnabled"] = False
        user_pref["MagnitudeThreshold"] = None
        upsert_user_magnitude_pref_to_sqlite(guild_id, user_id, False, None)

    def _add_user_state_alert(self, guild_id: str, user_id: str, state_code: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["States"].add(state_code)
        add_user_state_alert_to_sqlite(guild_id, user_id, state_code)

    def _remove_user_state_alert(self, guild_id: str, user_id: str, state_code: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["States"].discard(state_code)
        remove_user_state_alert_from_sqlite(guild_id, user_id, state_code)

    def _add_user_country_alert(self, guild_id: str, user_id: str, country_name: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["Countries"].add(country_name)
        add_user_country_alert_to_sqlite(guild_id, user_id, country_name)

    def _remove_user_country_alert(self, guild_id: str, user_id: str, country_name: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["Countries"].discard(country_name)
        remove_user_country_alert_from_sqlite(guild_id, user_id, country_name)

    def _get_matching_user_ids(self, guild: discord.Guild, eq_data: dict):
        guild_user_prefs = self.user_prefs.get(str(guild.id), {})
        if not guild_user_prefs:
            return []

        eq_magnitude = eq_data.get("magnitude")
        eq_states = infer_us_state_codes_from_place(eq_data.get("place", ""))
        eq_countries = infer_country_names_from_place(eq_data.get("place", ""), eq_states)
        matching_user_ids = set()

        for user_id, user_pref in guild_user_prefs.items():
            matches = False

            if user_pref.get("MagnitudeMentionEnabled"):
                threshold = user_pref.get("MagnitudeThreshold")
                if threshold is not None and eq_magnitude is not None and eq_magnitude >= threshold:
                    matches = True

            user_states = user_pref.get("States", set())
            if user_states and eq_states and user_states.intersection(eq_states):
                matches = True

            user_countries = user_pref.get("Countries", set())
            if user_countries and eq_countries and user_countries.intersection(eq_countries):
                matches = True

            if not matches:
                continue

            matching_user_ids.add(user_id)

        return sorted(matching_user_ids, key=int)


    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures eq_notify_db and guild_prefs are loaded before starting the polling loop."""

        # Create or set guild preferences
        for guild in self._iter_enabled_guilds():
            self._ensure_guild_pref(str(guild.id))

        # Set eq_notify_db guild.id default key object
        for guild in self._iter_enabled_guilds():
            if self.eq_notify_db.get(str(guild.id)):
                continue
            self.eq_notify_db[str(guild.id)] = {}

        save_guild_prefs_to_sqlite(self.guild_prefs)
        save_eq_notify_db_to_sqlite(self.eq_notify_db)
        self.poll_quakes.start()


    def _register_event_for_all_guilds(self, earthquake_id: str):
        for guild in self._iter_enabled_guilds():
            guild_id = str(guild.id)
            self.eq_notify_db.setdefault(guild_id, {})
            self.eq_notify_db[guild_id].setdefault(earthquake_id, False)

    def _pager_icon_for_level(self, pager_alert_level: str | None):
        match pager_alert_level:
            case "green":
                return "🟩"
            case "yellow":
                return "🟨"
            case "orange":
                return "🟧"
            case "red":
                return "🟥"
            case _:
                return "-"

    def _format_event_time(self, event_time_utc: datetime | None):
        if event_time_utc is None:
            return "unknown"
        dt = event_time_utc.astimezone(ZoneInfo("America/New_York"))
        return dt.strftime("%m/%d/%Y - %I:%M %p")

    def _event_to_eq_data(self, event: EarthquakeEvent):
        depth = "unknown" if event.depth_km is None else str(event.depth_km)
        return {
            "pager_lvl_icon": self._pager_icon_for_level(event.pager_alert_level),
            "place": event.place,
            "magnitude": event.magnitude,
            "url": event.url,
            "time": self._format_event_time(event.event_time_utc),
            "earthquake_id": event.earthquake_id,
            "pager_alert_level": event.pager_alert_level,
            "tsunami_potential": bool(event.tsunami_potential) if event.tsunami_potential is not None else False,
            "depth": depth,
            "latitude": event.latitude,
            "longitude": event.longitude,
            "significance": event.significance,
        }

    async def save_earthquakes(self, earthquakes: list[dict]):
        """Save all earthquake payloads to the eq_db object."""
        for eq_data in earthquakes:
            eq_id = eq_data.get("earthquake_id")
            if not eq_id:
                continue

            self._register_event_for_all_guilds(eq_id)
            if self.eq_db.get(eq_id):
                continue

            logging.info("||=*=|| Saving Earthquake ||=*=|| %s", eq_id)
            self.eq_db[eq_id] = {
                "pager_lvl_icon": eq_data["pager_lvl_icon"],
                "place": eq_data["place"],
                "magnitude": eq_data["magnitude"],
                "url": eq_data["url"],
                "time": eq_data["time"],
                "earthquake_id": eq_data["earthquake_id"],
                "pager_alert_level": eq_data["pager_alert_level"],
                "tsunami_potential": eq_data["tsunami_potential"],
                "depth": eq_data["depth"],
                "latitude": eq_data["latitude"],
                "longitude": eq_data["longitude"],
                "significance": eq_data["significance"],
            }
            logging.info("||=*=|| Earthquake Saved! ||=*=|| ")

    async def notify_guild(self, earthquakes: list[dict], guild: discord.Guild):
        if not self._is_enabled_guild(guild):
            return

        guild_id = str(guild.id)
        self._ensure_guild_pref(guild_id)
        if not self.eq_notify_db.get(guild_id):
            self.eq_notify_db[guild_id] = {}
        plot_style = self.guild_prefs[guild_id].get("PlotStyle", 0)
        channel = self._get_update_channel(guild)
        if not channel:
            return

        async def send_alert(eq_data: dict):
            image_path = f"eq_plot_{guild_id}_{sanitize_filename(eq_data['earthquake_id'])}.png"
            eq_embed, img_file = create_embed_quake_alert(
                eq_data,
                plot_style=plot_style,
                image_path=image_path,
            )
            mention_chunks = build_mention_chunks(self._get_matching_user_ids(guild, eq_data))
            first_message_mentions = mention_chunks[0] if mention_chunks else None
            try:
                sent_message = await channel.send(
                    content=first_message_mentions,
                    embed=eq_embed,
                    file=img_file,
                )
                for extra_chunk in mention_chunks[1:]:
                    await channel.send(extra_chunk)
                pin_threshold = self.guild_prefs[guild_id].get("PinMagnitude")
                eq_magnitude = eq_data.get("magnitude")
                if (
                    pin_threshold is not None
                    and eq_magnitude is not None
                    and float(eq_magnitude) >= float(pin_threshold)
                ):
                    try:
                        await sent_message.pin(
                            reason=f"Earthquake magnitude {eq_magnitude} meets pin threshold {pin_threshold}"
                        )
                    except discord.Forbidden:
                        logging.warning(
                            "Missing permission to pin messages in #%s for guild %s.",
                            channel.name,
                            guild.name,
                        )
                    except discord.HTTPException as err:
                        logging.warning(
                            "Failed to pin earthquake alert %s in guild %s: %s",
                            eq_data.get("earthquake_id"),
                            guild.name,
                            err,
                        )
            finally:
                if os.path.exists(image_path):
                    os.remove(image_path)
            self.eq_notify_db[guild_id][eq_data["earthquake_id"]] = True

        for eq_data in earthquakes:
            if self.eq_notify_db[guild_id].get(eq_data["earthquake_id"]):
                continue

            if earthquake_passes_guild_magnitude_filter(
                eq_data,
                self.guild_prefs[guild_id]["MinMagnitude"],
            ):
                await send_alert(eq_data)
                continue

            if self.guild_prefs[guild_id]["MinMagnitude"] == 4:
                logging.info(
                    "Significant only quakes selected,"
                    " but not configured(how to parse these from the hourly all eq feed?)"
                )
                continue

            threshold_label = {
                1: "1.0",
                2: "2.5",
                3: "4.5",
            }.get(self.guild_prefs[guild_id]["MinMagnitude"], "unknown")
            logging.info(
                "Magnitude %s is too low (<%s), skipping message for guild: %s",
                eq_data.get("magnitude"),
                threshold_label,
                guild,
            )


    @tasks.loop(seconds=60.0)
    async def poll_quakes(self):
        """Every 10 minutes, poll the api for new earthquakes. When a new quake is detected,
         add the quake_id to the eq_notify_db dictionary. If eq_notify_db.guildid.quakeid = false,
         that guild has not reported on the eq If the quake is in the dict, it will be ignored."""
        print("Guild prefs from start of poll quakes cmd: ", self.guild_prefs)
        start_time = time.perf_counter()
        logging.info("%s function initiated.", colorize("poll_quakes", "blue"))

        # First fetch latest eq_db
        self.eq_db = get_eq_db()
        logging.info("%s loaded! %s total earthquakes on record.",
                     colorize("eq_db", "yellow"),
                     len(self.eq_db))

        await self.client.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            source_events = await fetch_events_from_sources(session, self.event_sources)

        if len(source_events) == 0:
            logging.info("No earthquakes detected from configured sources.")
            return

        earthquake_payloads = [self._event_to_eq_data(event) for event in source_events]

        # Save earthquakes once after fetching all configured sources.
        await self.save_earthquakes(earthquake_payloads)

        for guild in self._iter_enabled_guilds():
            logging.info("Attempting to notify guild: %s", guild.name)
            await self.notify_guild(earthquake_payloads, guild)
            logging.info("Completed notifying guild: %s", guild.name)

        end_time = time.perf_counter()

        save_eq_db_to_sqlite(self.eq_db)
        save_eq_notify_db_to_sqlite(self.eq_notify_db)
        logging.info("%s function completed. Elapsed %.2f seconds.",
                     colorize("poll_quakes", "blue"),
                     end_time - start_time)

    @commands.hybrid_command(name="top-10-largest-today",aliases=["top10"])
    async def top10(self, ctx: commands.Context):
        """Send an embed message to the channel containing
         the top 10 earthquakes that occurred on today's date."""
        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date  # Parse dates out of the time col
        today = datetime.today().date()  # Get the date
        today_str = today.strftime("%B %d, %Y")  # Get str of today's date
        df_today = df[df["date"] == today].copy()  # Filter for today's data only

        embed = discord.Embed(
            title=f"Top 10 Largest Earthquakes {today_str}",
            color=discord.Color.gold(),
            )

        df_today.sort_values(by=["magnitude"], ascending=False, inplace=True)
        top10: pd.DataFrame = df_today[['place', 'magnitude']].head(10)

        line_count = 1
        for _, row in top10.iterrows():
            formatted_line = f"{row['place']:<50} | MAG:{row['magnitude']:>4}"
            embed.add_field(name=f"#{line_count}", value=formatted_line, inline=False)
            line_count += 1

        await ctx.send(embed=embed)


    @commands.hybrid_command(name="top-10-largest-30days", aliases=["top10month"])
    async def top10month(self, ctx: commands.Context):
        """Send an embed message to the channel containing
         the top 10 earthquakes from the last 30 days."""
        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])

        now = datetime.now()
        cutoff = now - timedelta(days=30)
        df_last_30 = df[df["time"] >= cutoff].copy()

        embed = discord.Embed(
            title="Top 10 Largest Earthquakes (Last 30 Days)",
            color=discord.Color.gold(),
        )

        df_last_30.sort_values(by=["magnitude"], ascending=False, inplace=True)
        top10: pd.DataFrame = df_last_30[['place', 'magnitude']].head(10)

        line_count = 1
        for _, row in top10.iterrows():
            formatted_line = f"{row['place']:<50} | MAG:{row['magnitude']:>4}"
            embed.add_field(name=f"#{line_count}", value=formatted_line, inline=False)
            line_count += 1

        await ctx.send(embed=embed)


    @commands.hybrid_command(name="today")
    async def today(self, ctx: commands.Context):
        """Output a summary of today's earthquakes.
        Plot all earthquakes to a map and send as message."""
        if ctx.interaction is not None:
            await ctx.defer()

        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today]  # Filter for today's data only

        plot_style = 0
        if ctx.guild is not None:
            self._ensure_guild_pref(str(ctx.guild.id))
            plot_style = self.guild_prefs[str(ctx.guild.id)]["PlotStyle"]
        plot_daily_earthquakes(df_today, plot_style=plot_style)
        title_str = (f"Total Eathquakes Today: {len(df_today)}\n "
                     f">= Magnitude 3: {len(df_today[df_today['magnitude'] >= 3.0])}")
        embed = discord.Embed(
            title=title_str,
            color=discord.Color.gold(),
        )

        embed.add_field(name="Average Magnitude",
                        value=f"{df_today['magnitude'].mean():.2f}",
                        inline=True)
        embed.add_field(name="Highest Magnitude",
                        value=f"{df_today['magnitude'].max():.2f}",
                        inline=True)
        embed.add_field(name="Lowest Magnitude",
                        value=f"{df_today['magnitude'].min():.2f}",
                        inline=True)

        img_file = discord.File("eq_plot_all_today.png", filename="earthquake.png")
        embed.set_image(url="attachment://earthquake.png")

        await ctx.send(embed=embed, file=img_file)


    @commands.hybrid_command(name="maptoday")
    async def maptoday(self, ctx: commands.Context):
        """Plot all earthquakes from today on a flat map and send as message."""
        if ctx.interaction is not None:
            await ctx.defer()

        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today].copy()

        plot_style = 0
        if ctx.guild is not None:
            self._ensure_guild_pref(str(ctx.guild.id))
            plot_style = self.guild_prefs[str(ctx.guild.id)]["PlotStyle"]
        plot_daily_earthquakes(df_today, plot_style=plot_style)

        embed = discord.Embed(
            title=f"Earthquakes Today ({today.strftime('%B %d, %Y')})",
            color=discord.Color.gold(),
        )

        img_file = discord.File("eq_plot_all_today.png", filename="earthquake.png")
        embed.set_image(url="attachment://earthquake.png")

        await ctx.send(embed=embed, file=img_file)


    @commands.hybrid_command(name="export-earthquakes", aliases=["exporteqcsv", "exportcsv"])
    async def export_earthquakes(self, ctx: commands.Context):
        """Export all earthquake records to CSV and upload files to the current channel."""
        if ctx.interaction is not None:
            await ctx.defer()

        export_data = fetch_earthquake_export_data()
        total_quakes = export_data["count"]

        if total_quakes == 0:
            await ctx.send("No earthquake data is available to export.")
            return

        try:
            csv_parts = build_earthquake_csv_parts(export_data["rows"])
        except ValueError as err:
            await ctx.send(f"Export failed: {err}")
            return

        split_count = len(csv_parts)
        total_csv_bytes = sum(len(part["bytes"]) for part in csv_parts)
        avg_mag = export_data["avg_magnitude"]
        min_mag = export_data["min_magnitude"]
        max_mag = export_data["max_magnitude"]

        avg_mag_str = f"{avg_mag:.2f}" if avg_mag is not None else "N/A"
        min_mag_str = f"{min_mag:.2f}" if min_mag is not None else "N/A"
        max_mag_str = f"{max_mag:.2f}" if max_mag is not None else "N/A"

        summary = (
            f"Earthquake export ready.\n"
            f"Rows: {total_quakes:,}\n"
            f"Average magnitude: {avg_mag_str}\n"
            f"Min magnitude: {min_mag_str}\n"
            f"Max magnitude: {max_mag_str}\n"
            f"Total CSV size: {format_bytes(total_csv_bytes)} ({total_csv_bytes:,} bytes)\n"
            f"Split count: {split_count} file(s)\n"
            f"Per-file limit: {format_bytes(DISCORD_FILE_LIMIT_BYTES)} hard max"
        )
        await ctx.send(summary)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for idx, part in enumerate(csv_parts, start=1):
            filename = f"earthquakes_export_{timestamp}_part{idx:03d}.csv"
            payload = io.BytesIO(part["bytes"])
            csv_file = discord.File(payload, filename=filename)
            await ctx.send(
                content=(
                    f"Export file {idx}/{split_count} | "
                    f"Rows: {part['row_count']:,} | "
                    f"Size: {format_bytes(len(part['bytes']))} ({len(part['bytes']):,} bytes)"
                ),
                file=csv_file,
            )

    @commands.hybrid_group(name="alert-me", aliases=["alertme"], invoke_without_command=True)
    async def alert_me(self, ctx: commands.Context):
        """Manage personal earthquake alerts for this guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if ctx.invoked_subcommand is not None:
            return

        await ctx.send(
            "Use one of these subcommands:\n"
            "/alert-me magnitude <threshold>\n"
            "/alert-me disable-magnitude\n"
            "/alert-me add-state <state>\n"
            "/alert-me remove-state <state>\n"
            "/alert-me add-country <country>\n"
            "/alert-me remove-country <country>\n"
            "/my-alerts"
        )

    @alert_me.command(name="magnitude")
    async def alert_me_magnitude(self, ctx: commands.Context, threshold: float):
        """Enable personal mention alerts for earthquakes above a magnitude threshold."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if threshold < 0 or threshold > 10:
            await ctx.send("Magnitude threshold must be between 0.0 and 10.0.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        self._set_user_magnitude_alert(guild_id, user_id, threshold)
        await ctx.send(
            f"Personal magnitude mentions enabled for <@{user_id}> at M{threshold:.1f}+ in this server."
        )

    @alert_me.command(name="disable-magnitude")
    async def alert_me_disable_magnitude(self, ctx: commands.Context):
        """Disable personal magnitude mention alerts."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        self._disable_user_magnitude_alert(guild_id, user_id)
        await ctx.send("Personal magnitude mention alerts are now disabled.")

    @alert_me.command(name="add-state")
    async def alert_me_add_state(self, ctx: commands.Context, *, state: str):
        """Subscribe to personal alerts for a US state (abbr or full name)."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        state_code = normalize_state_code(state)
        if state_code is None:
            await ctx.send(
                "Invalid US state. Use a 2-letter code like `OH` or full name like `Ohio`."
            )
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if state_code in user_pref["States"]:
            await ctx.send(
                f"You are already subscribed to {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
            )
            return

        self._add_user_state_alert(guild_id, user_id, state_code)
        await ctx.send(
            f"Added personal state alert for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
        )

    @alert_me.command(name="remove-state")
    async def alert_me_remove_state(self, ctx: commands.Context, *, state: str):
        """Unsubscribe from personal alerts for a US state (abbr or full name)."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        state_code = normalize_state_code(state)
        if state_code is None:
            await ctx.send(
                "Invalid US state. Use a 2-letter code like `OH` or full name like `Ohio`."
            )
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if state_code not in user_pref["States"]:
            await ctx.send(
                f"You do not have a state alert configured for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
            )
            return

        self._remove_user_state_alert(guild_id, user_id, state_code)
        await ctx.send(
            f"Removed personal state alert for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
        )

    @alert_me.command(name="add-country")
    async def alert_me_add_country(self, ctx: commands.Context, *, country: str):
        """Subscribe to personal alerts for a country."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        country_name = normalize_country_name(country)
        if country_name is None:
            await ctx.send("Invalid country name. Example: `Iran`, `Chile`, `United States`.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if country_name in user_pref["Countries"]:
            await ctx.send(f"You are already subscribed to country alerts for {country_name}.")
            return

        self._add_user_country_alert(guild_id, user_id, country_name)
        await ctx.send(f"Added personal country alert for {country_name}.")

    @alert_me.command(name="remove-country")
    async def alert_me_remove_country(self, ctx: commands.Context, *, country: str):
        """Unsubscribe from personal alerts for a country."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        country_name = normalize_country_name(country)
        if country_name is None:
            await ctx.send("Invalid country name. Example: `Iran`, `Chile`, `United States`.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if country_name not in user_pref["Countries"]:
            await ctx.send(f"You do not have a country alert configured for {country_name}.")
            return

        self._remove_user_country_alert(guild_id, user_id, country_name)
        await ctx.send(f"Removed personal country alert for {country_name}.")

    @commands.hybrid_command(name="my-alerts")
    async def my_alerts(self, ctx: commands.Context):
        """List your personal alert settings for this guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)

        if user_pref["MagnitudeMentionEnabled"] and user_pref["MagnitudeThreshold"] is not None:
            magnitude_text = f"enabled at M{user_pref['MagnitudeThreshold']:.1f}+"
        else:
            magnitude_text = "disabled"

        state_codes = sorted(user_pref["States"])
        if state_codes:
            state_text = ", ".join(
                f"{state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})"
                for state_code in state_codes
            )
        else:
            state_text = "none"

        country_names = sorted(user_pref["Countries"])
        country_text = ", ".join(country_names) if country_names else "none"

        await ctx.send(
            f"Personal earthquake alerts for <@{user_id}> in **{ctx.guild.name}**\n"
            f"- Magnitude mentions: {magnitude_text}\n"
            f"- State subscriptions: {state_text}\n"
            f"- Country subscriptions: {country_text}\n"
            "- State/country matching is inferred from USGS `place` text."
        )

    @commands.hybrid_command(name="config_map")
    async def config_map(self, ctx: commands.Context):
        """Configure the map style for this guild with reaction-based style previews."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if ctx.interaction is not None:
            await ctx.defer()

        if len(MAP_STYLE_OPTIONS) > len(NUMBER_REACTIONS):
            await ctx.send("Too many map styles are configured for the available number reactions.")
            return

        guild_id = str(ctx.guild.id)
        self._ensure_guild_pref(guild_id)

        files = []
        embeds = []
        preview_paths = []
        style_lines = []
        reaction_to_style = {}

        try:
            for idx, style in enumerate(MAP_STYLE_OPTIONS):
                emoji = NUMBER_REACTIONS[idx]
                reaction_to_style[emoji] = idx
                style_lines.append(f"{emoji} | {style['label']} (`{style['map_style']}`)")

                preview_path = f"map_style_preview_{guild_id}_{idx}.png"
                preview_paths.append(preview_path)

                plot_to_img_with_plotly(
                    DEFAULT_MAP_LONGITUDE,
                    DEFAULT_MAP_LATITUDE,
                    "Example Location",
                    "Preview",
                    filename=preview_path,
                    plot_style=idx,
                )

                attachment_name = f"map_style_{idx}.png"
                files.append(discord.File(preview_path, filename=attachment_name))

                embed = discord.Embed(
                    title=f"{emoji} {style['label']}",
                    description=f"`{style['map_style']}`",
                    color=discord.Color.blurple(),
                )
                embed.set_image(url=f"attachment://{attachment_name}")
                embeds.append(embed)
        except Exception as err:
            for path in preview_paths:
                if os.path.exists(path):
                    os.remove(path)
            await ctx.send(f"Unable to generate map style previews: {err}")
            return

        prompt = (
            "Choose a map style for this server by reacting with a number.\n"
            "Preview coordinate: (-74.00, 40.71)\n\n"
            + "\n".join(style_lines)
        )

        try:
            await self._send_map_style_preview_batches(ctx, embeds, files)
            msg = await ctx.send(content=prompt)
        finally:
            for path in preview_paths:
                if os.path.exists(path):
                    os.remove(path)

        for emoji in reaction_to_style:
            await msg.add_reaction(emoji)

        def check(reaction, usr):
            return (
                usr.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in reaction_to_style
            )

        try:
            reaction, usr = await self.client.wait_for(
                "reaction_add",
                timeout=120.0,
                check=check,
            )
            logging.info("Reaction : %s, user: %s", reaction, usr.name)
        except asyncio.TimeoutError:
            await ctx.send("Timed out. You must re-run /config_map and react within 2 minutes.")
            return

        selected_style_idx = reaction_to_style[str(reaction.emoji)]
        self.guild_prefs[guild_id]["PlotStyle"] = selected_style_idx
        save_guild_prefs_to_sqlite(self.guild_prefs)
        selected_style, _ = get_map_style_option(selected_style_idx)
        await ctx.send(
            f"Map style set to {selected_style['label']} (`{selected_style['map_style']}`) "
            "for this server."
        )

    @commands.hybrid_command(name="channel")
    async def channel(self, ctx: commands.Context, channel_name: str):
        """Set the text channel used for earthquake updates in this guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        normalized_name = str(channel_name).strip().lstrip("#")
        if not normalized_name:
            await ctx.send("Please provide a text channel name, for example `/channel quake-updates`.")
            return

        channel = self._find_text_channel_by_name(ctx.guild, normalized_name)
        if channel is None:
            await ctx.send(f"I couldn't find a text channel named `#{normalized_name}` in this server.")
            return

        guild_id = str(ctx.guild.id)
        self._ensure_guild_pref(guild_id)
        self.guild_prefs[guild_id]["UpdateChannelId"] = channel.id
        save_guild_prefs_to_sqlite(self.guild_prefs)
        await ctx.send(f"Earthquake updates will now be sent to {channel.mention}.")


    @commands.hybrid_command(name="config")
    async def config(self, ctx: commands.Context):
        """Configure the bots settings for a specific guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        self._ensure_guild_pref(str(ctx.guild.id))

        msg = await ctx.send(
            "Please choose a minimum magnitude to report on.\n\n"
            "0️⃣ | All earthquakes (Caution.. lots)\n"
            "1️⃣ | 1.0 and above\n"
            "2️⃣ | 2.5 and above\n"
            "3️⃣ | 4.5 and above\n"
            "4️⃣ | Significant only"
        )
        reactions = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣"]

        for reaction in reactions:
            await msg.add_reaction(reaction)

        try:
            def check(reaction, usr):
                return (
                    usr.id == ctx.author.id
                    and reaction.message.id == msg.id
                    and str(reaction.emoji) in reactions
                )

            reaction, usr = await self.client.wait_for(
                "reaction_add",
                timeout=60.0,
                check=check,
            )

            logging.info(f"Reaction : %s, user: %s", reaction, usr.name)
            emoji = str(reaction.emoji)

        except asyncio.TimeoutError:
            await ctx.send("Timed out. You must re-run .config , and react within 1 minute.")
            return

        match emoji:
            case "0️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 0
                await ctx.send("All earthquakes are being reported on by the minute.")
            case "1️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 1
                await ctx.send("1.0+ earthquakes are being reported on by the minute.")
            case "2️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 2
                await ctx.send("2.5+ earthquakes are being reported on by the minute.")
            case "3️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 3
                await ctx.send("4.5+ earthquakes are being reported on by the minute.")
            case "4️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 4
                await ctx.send("Only significant earthquakes are being reported on by the minute.")
            case _:
                await ctx.send("That is not a valid emoji.")

        save_guild_prefs_to_sqlite(self.guild_prefs)
        await msg.delete()

    @commands.hybrid_command(name="pin-magnitude")
    async def pin_magnitude(self, ctx: commands.Context, magnitude: float):
        """Set the minimum earthquake magnitude that triggers pinning alert messages."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if magnitude < 0:
            await ctx.send("Please provide a non-negative magnitude.")
            return

        guild_id = str(ctx.guild.id)
        self._ensure_guild_pref(guild_id)
        self.guild_prefs[guild_id]["PinMagnitude"] = float(magnitude)
        save_guild_prefs_to_sqlite(self.guild_prefs)
        await ctx.send(
            f"Earthquake alerts with magnitude **{magnitude:g}+** will now be pinned in their alert channel."
        )


async def setup(bot):
    """Called when the cog is loaded."""
    await bot.add_cog(LiveTracking(bot))
