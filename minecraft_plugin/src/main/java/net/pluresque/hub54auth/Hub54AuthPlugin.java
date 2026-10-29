package net.pluresque.hub54auth;

import io.papermc.paper.chat.ChatRenderer;
import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextColor;
import net.kyori.adventure.text.format.TextDecoration;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import net.kyori.adventure.title.Title;
import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.Sound;
import org.bukkit.SoundCategory;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityPickupItemEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.player.AsyncPlayerPreLoginEvent;
import org.bukkit.event.player.PlayerCommandPreprocessEvent;
import org.bukkit.event.player.PlayerDropItemEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerSwapHandItemsEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

import java.net.InetAddress;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.SQLException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;

public final class Hub54AuthPlugin extends JavaPlugin implements Listener {
    private record ConnectionContext(Player player, String addressFingerprint) {
    }

    private static final TextColor SUCCESS_COLOR = TextColor.color(0x90EE90);
    private static final TextColor TELEGRAM_COLOR = TextColor.color(0xFF8C00);
    private static final Set<String> PRIVATE_MESSAGE_ALIASES = Set.of(
            "msg", "tell", "w", "whisper", "message", "m"
    );

    private final ConcurrentHashMap<UUID, AtomicInteger> failedAttempts = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<UUID, Long> lastAttemptAt = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<UUID, BukkitTask> loginPromptTasks = new ConcurrentHashMap<>();
    private final java.util.Set<UUID> verifying = ConcurrentHashMap.newKeySet();
    private final java.util.Set<UUID> authenticated = ConcurrentHashMap.newKeySet();
    private final Set<UUID> resumedSessions = ConcurrentHashMap.newKeySet();
    private final ConcurrentHashMap<UUID, ConnectionContext> connectionContexts =
            new ConcurrentHashMap<>();
    private final AtomicBoolean telegramChatPollRunning = new AtomicBoolean();
    private final AtomicBoolean telegramPrivateMessagePollRunning = new AtomicBoolean();
    private volatile AccountStore accountStore;
    private Path databasePath;
    private boolean databaseWarningLogged;
    private int maximumAttempts;
    private long sessionDurationSeconds;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        maximumAttempts = Math.max(1, getConfig().getInt("maximum-password-attempts", 5));
        int sessionDurationDays = Math.max(
                1,
                Math.min(365, getConfig().getInt("session-duration-days", 30))
        );
        sessionDurationSeconds = Duration.ofDays(sessionDurationDays).toSeconds();
        databasePath = Path.of(
                getConfig().getString("database-path", "/botdata/minecraft_bot.sqlite3")
        );
        Bukkit.getPluginManager().registerEvents(this, this);
        connectDatabase();
        Bukkit.getScheduler().runTaskTimer(this, () -> {
            if (accountStore == null) {
                connectDatabase();
            }
            syncWhitelist();
        }, 20L * 5, 20L * 15);
        Bukkit.getScheduler().runTaskTimerAsynchronously(
                this, this::pollTelegramChat, 20L, 20L
        );
        Bukkit.getScheduler().runTaskTimerAsynchronously(
                this, this::pollTelegramPrivateMessages, 20L, 20L
        );
        Bukkit.getScheduler().runTaskTimer(this, this::updateTabHeaders, 20L, 20L);
    }

    @Override
    public void onDisable() {
        AccountStore store = accountStore;
        if (store == null) {
            return;
        }
        try {
            store.setPrivateMessageBridgeEnabled(false);
        } catch (SQLException exception) {
            getLogger().log(Level.WARNING,
                    "Не удалось выключить мост личных сообщений.", exception);
        }
    }

    private void connectDatabase() {
        if (accountStore != null) {
            return;
        }
        try {
            accountStore = new AccountStore(databasePath);
            accountStore.setPrivateMessageBridgeEnabled(true);
            databaseWarningLogged = false;
            syncWhitelist();
            getLogger().info("Плагин авторизации подключён к базе Telegram бота.");
        } catch (Exception exception) {
            if (!databaseWarningLogged) {
                getLogger().log(Level.WARNING,
                        "База бота пока недоступна. Вход останется закрыт до подключения базы.",
                        exception);
                databaseWarningLogged = true;
            }
        }
    }

    @EventHandler(priority = EventPriority.LOWEST)
    public void onPreLogin(AsyncPlayerPreLoginEvent event) {
        UUID playerId = event.getUniqueId();
        resumedSessions.remove(playerId);
        AccountStore store = accountStore;
        if (store == null) {
            event.disallow(
                    AsyncPlayerPreLoginEvent.Result.KICK_OTHER,
                    Component.text("Сервис авторизации запускается. Попробуйте войти позже.")
            );
            return;
        }
        try {
            Optional<Account> account = store.findAllowedAccount(event.getName());
            if (account.isEmpty()) {
                event.disallow(
                        AsyncPlayerPreLoginEvent.Result.KICK_WHITELIST,
                        Component.text("Аккаунт не найден в боте. Сначала запросите доступ.")
                );
                return;
            }
            if (account.get().passwordHash() == null || account.get().passwordHash().isBlank()) {
                event.disallow(
                        AsyncPlayerPreLoginEvent.Result.KICK_OTHER,
                        Component.text("Сначала задайте пароль в Telegram боте.")
                );
                return;
            }

            InetAddress address = event.getAddress();
            if (address != null) {
                String fingerprint = addressFingerprint(address);
                long now = System.currentTimeMillis() / 1000L;
                if (store.resumeSession(
                        playerId,
                        account.get().telegramId(),
                        account.get().passwordHash(),
                        fingerprint,
                        now,
                        now + sessionDurationSeconds
                )) {
                    resumedSessions.add(playerId);
                }
            }
        } catch (SQLException exception) {
            getLogger().log(Level.SEVERE, "Ошибка чтения профиля при входе.", exception);
            event.disallow(
                    AsyncPlayerPreLoginEvent.Result.KICK_OTHER,
                    Component.text("Сервис авторизации временно недоступен. Попробуйте позже.")
            );
        }
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        UUID playerId = player.getUniqueId();
        event.joinMessage(null);
        player.sendPlayerListHeader(Component.empty());
        var socketAddress = player.getAddress();
        InetAddress remoteAddress = socketAddress == null ? null : socketAddress.getAddress();
        String fingerprint = remoteAddress == null ? null : addressFingerprint(remoteAddress);
        connectionContexts.put(playerId, new ConnectionContext(player, fingerprint));
        stopLoginPrompt(playerId);
        if (resumedSessions.remove(playerId)) {
            authenticated.add(playerId);
            failedAttempts.remove(playerId);
            lastAttemptAt.remove(playerId);
            player.setWalkSpeed(0.2f);
            updateTabHeader(player, currentTps());
            player.sendMessage(Component.text("Сессия подтверждена.", SUCCESS_COLOR));
            announceAuthenticatedJoin(player);
            return;
        }
        authenticated.remove(playerId);
        failedAttempts.put(playerId, new AtomicInteger());
        player.setWalkSpeed(0.0f);
        showLoginPrompt(player);
        BukkitTask promptTask = Bukkit.getScheduler().runTaskTimer(
                this,
                () -> {
                    if (player.isOnline() && !isAuthenticated(player)) {
                        showLoginPrompt(player);
                    }
                },
                100L,
                100L
        );
        loginPromptTasks.put(playerId, promptTask);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onChat(AsyncChatEvent event) {
        Player player = event.getPlayer();
        UUID playerId = player.getUniqueId();
        if (authenticated.contains(playerId)) {
            String senderName = player.getName();
            event.renderer(ChatRenderer.viewerUnaware((source, sourceDisplayName, message) ->
                    Component.text(senderName, NamedTextColor.WHITE)
                            .append(Component.text(": ", NamedTextColor.DARK_GRAY))
                            .append(message)));
            return;
        }

        event.setCancelled(true);
        String input = PlainTextComponentSerializer.plainText().serialize(event.message());
        if (input.isBlank() || input.length() > 128) {
            player.sendMessage(Component.text("Пароль должен содержать от 4 до 128 символов."));
            return;
        }

        long now = System.currentTimeMillis();
        Long previous = lastAttemptAt.put(playerId, now);
        if (previous != null && now - previous < 750) {
            player.sendMessage(Component.text("Подождите перед следующей попыткой."));
            return;
        }
        if (!verifying.add(playerId)) {
            player.sendMessage(Component.text("Проверяю пароль. Подождите."));
            return;
        }

        AtomicInteger attempts = failedAttempts.computeIfAbsent(playerId, ignored -> new AtomicInteger());
        int attemptNumber = attempts.incrementAndGet();
        if (attemptNumber > maximumAttempts) {
            verifying.remove(playerId);
            Bukkit.getScheduler().runTask(this, () -> {
                if (player.isOnline()) {
                    player.kick(Component.text("Слишком много попыток. Подключитесь снова."));
                }
            });
            return;
        }

        ConnectionContext connection = connectionContexts.get(playerId);
        String fingerprint = connection != null && connection.player() == player
                ? connection.addressFingerprint()
                : null;
        char[] password = input.toCharArray();
        Bukkit.getScheduler().runTaskAsynchronously(
                this,
                () -> verifyPassword(player, password, fingerprint)
        );
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onAuthenticatedChat(AsyncChatEvent event) {
        Player player = event.getPlayer();
        if (!isAuthenticated(player)) {
            return;
        }
        String message = PlainTextComponentSerializer.plainText().serialize(event.message());
        publishNotification("chat", player.getName() + ": " + message);
    }

    private void verifyPassword(Player player, char[] password, String fingerprint) {
        UUID playerId = player.getUniqueId();
        boolean accepted = false;
        boolean databaseError = false;
        try {
            Optional<Account> account = accountStore.findAllowedAccount(player.getName());
            if (account.isPresent() && PasswordVerifier.verify(password, account.get().passwordHash())) {
                if (fingerprint == null) {
                    accountStore.recordSuccessfulLoginWithoutSession(account.get().telegramId());
                    getLogger().warning(
                            "Пароль игрока " + player.getName()
                                    + " принят без сохранения сессии: адрес недоступен."
                    );
                } else {
                    long now = System.currentTimeMillis() / 1000L;
                    accountStore.recordSuccessfulLogin(
                            account.get().telegramId(),
                            playerId,
                            fingerprint,
                            now + sessionDurationSeconds
                    );
                }
                accepted = true;
            }
        } catch (Exception exception) {
            databaseError = true;
            getLogger().log(Level.SEVERE, "Не удалось проверить пароль игрока.", exception);
        } finally {
            Arrays.fill(password, '\0');
        }

        boolean loginAccepted = accepted;
        boolean hasDatabaseError = databaseError;
        Bukkit.getScheduler().runTask(this, () -> {
            verifying.remove(playerId);
            if (!player.isOnline()) {
                return;
            }
            if (hasDatabaseError) {
                player.kick(Component.text("Не удалось проверить пароль. Попробуйте позже."));
                return;
            }
            if (loginAccepted) {
                authenticated.add(playerId);
                failedAttempts.remove(playerId);
                lastAttemptAt.remove(playerId);
                stopLoginPrompt(playerId);
                player.clearTitle();
                player.setWalkSpeed(0.2f);
                updateTabHeader(player, currentTps());
                player.sendMessage(
                        Component.text("Вход выполнен. Добро пожаловать!", SUCCESS_COLOR)
                );
                player.playSound(
                        player.getLocation(),
                        Sound.ENTITY_PLAYER_LEVELUP,
                        SoundCategory.PLAYERS,
                        0.65f,
                        1.15f
                );
                announceAuthenticatedJoin(player);
                return;
            }

            int attempts = failedAttempts.getOrDefault(playerId, new AtomicInteger()).get();
            if (attempts >= maximumAttempts) {
                player.kick(Component.text("Пароль неверный. Подключитесь снова."));
            } else {
                player.sendMessage(Component.text("Пароль неверный. Попробуйте ещё раз."));
            }
        });
    }

    @EventHandler(ignoreCancelled = true)
    public void onMove(PlayerMoveEvent event) {
        if (!isAuthenticated(event.getPlayer()) && event.hasChangedPosition()) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onCommand(PlayerCommandPreprocessEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
            showLoginPrompt(event.getPlayer());
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void syncPrivateMessageWithTelegram(PlayerCommandPreprocessEvent event) {
        Player sender = event.getPlayer();
        if (!isAuthenticated(sender)) {
            return;
        }

        String rawCommand = event.getMessage();
        int separator = rawCommand.indexOf(' ');
        String commandName = (separator < 0
                ? rawCommand.substring(1)
                : rawCommand.substring(1, separator)).toLowerCase(Locale.ROOT);
        int namespaceSeparator = commandName.lastIndexOf(':');
        if (namespaceSeparator >= 0) {
            commandName = commandName.substring(namespaceSeparator + 1);
        }
        if (!PRIVATE_MESSAGE_ALIASES.contains(commandName)) {
            return;
        }

        String arguments = separator < 0 ? "" : rawCommand.substring(separator + 1).trim();
        int targetSeparator = arguments.indexOf(' ');
        if (targetSeparator < 0 || targetSeparator == arguments.length() - 1) {
            return;
        }
        String targetName = arguments.substring(0, targetSeparator);
        String message = arguments.substring(targetSeparator + 1).trim();
        if (message.isEmpty() || message.length() > 256) {
            return;
        }

        Player target = Bukkit.getOnlinePlayers().stream()
                .filter(player -> player.getName().equalsIgnoreCase(targetName))
                .findFirst()
                .orElse(null);
        if (target == null || !isAuthenticated(target)
                || target.getUniqueId().equals(sender.getUniqueId())) {
            return;
        }

        AccountStore store = accountStore;
        if (store == null) {
            return;
        }
        String senderName = sender.getName();
        String recipientName = target.getName();
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            try {
                store.enqueueMinecraftPrivateMessage(senderName, recipientName, message);
            } catch (SQLException exception) {
                getLogger().log(Level.WARNING,
                        "Не удалось передать личное сообщение в Telegram.", exception);
            }
        });
    }

    @EventHandler(ignoreCancelled = true)
    public void onBlockBreak(BlockBreakEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onBlockPlace(BlockPlaceEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onInteract(PlayerInteractEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onEntityInteract(PlayerInteractEntityEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onDrop(PlayerDropItemEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onInventoryClick(InventoryClickEvent event) {
        if (event.getWhoClicked() instanceof Player player && !isAuthenticated(player)) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onSwapHands(PlayerSwapHandItemsEvent event) {
        if (!isAuthenticated(event.getPlayer())) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        if (event.getEntity() instanceof Player player && !isAuthenticated(player)) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onAttack(EntityDamageByEntityEvent event) {
        if (event.getDamager() instanceof Player player && !isAuthenticated(player)) {
            event.setCancelled(true);
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onPlayerDeath(PlayerDeathEvent event) {
        Player victim = event.getEntity();
        if (!isAuthenticated(victim)) {
            return;
        }

        publishNotification("death", victim.getName() + " погиб.");
        Player killer = victim.getKiller();
        if (killer != null && isAuthenticated(killer)) {
            publishNotification("kill", killer.getName() + " убил " + victim.getName() + ".");
        }
    }

    @EventHandler(ignoreCancelled = true)
    public void onPickupItem(EntityPickupItemEvent event) {
        if (event.getEntity() instanceof Player player && !isAuthenticated(player)) {
            event.setCancelled(true);
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        Player player = event.getPlayer();
        UUID playerId = player.getUniqueId();
        event.quitMessage(null);
        ConnectionContext connection = connectionContexts.get(playerId);
        if (connection != null && connection.player() != player) {
            return;
        }
        if (connection != null) {
            connectionContexts.remove(playerId, connection);
        }
        stopLoginPrompt(playerId);
        boolean wasAuthenticated = authenticated.remove(playerId);
        if (wasAuthenticated) {
            broadcastToAuthenticatedPlayers(
                    Component.text(
                            event.getPlayer().getName() + " вышел с сервера.",
                            NamedTextColor.YELLOW
                    )
            );
            publishNotification("leave", event.getPlayer().getName() + " вышел с сервера.");
        }
        failedAttempts.remove(playerId);
        lastAttemptAt.remove(playerId);
        verifying.remove(playerId);
        // Next pre-login replaces this marker. Keep it across quit to avoid
        // an older connection's quit event clearing a newer login's session.
    }

    private boolean isAuthenticated(Player player) {
        return authenticated.contains(player.getUniqueId());
    }

    private void showLoginPrompt(Player player) {
        player.showTitle(Title.title(
                Component.text("Введи пароль", NamedTextColor.AQUA),
                Component.text(
                        "Отправь его в чат. Другие игроки не увидят пароль.",
                        NamedTextColor.WHITE
                ),
                Title.Times.times(
                        Duration.ofMillis(150),
                        Duration.ofSeconds(5),
                        Duration.ofMillis(250)
                )
        ));
    }

    private void stopLoginPrompt(UUID playerId) {
        BukkitTask task = loginPromptTasks.remove(playerId);
        if (task != null) {
            task.cancel();
        }
    }

    private void announceAuthenticatedJoin(Player player) {
        broadcastToAuthenticatedPlayers(
                Component.text(
                        player.getName() + " присоединился к серверу.",
                        NamedTextColor.YELLOW
                )
        );
        publishNotification("join", player.getName() + " вошёл на сервер.");
    }

    private static String addressFingerprint(InetAddress address) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(address.getAddress());
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 недоступен.", exception);
        }
    }

    private void broadcastToAuthenticatedPlayers(Component message) {
        for (Player player : Bukkit.getOnlinePlayers()) {
            if (isAuthenticated(player)) {
                player.sendMessage(message);
            }
        }
    }

    private void updateTabHeaders() {
        double tps = currentTps();
        for (Player player : Bukkit.getOnlinePlayers()) {
            if (isAuthenticated(player)) {
                updateTabHeader(player, tps);
            }
        }
    }

    private static double currentTps() {
        double[] samples = Bukkit.getTPS();
        return samples.length == 0 ? 20.0 : Math.max(0.0, samples[0]);
    }

    private static void updateTabHeader(Player player, double tps) {
        String tpsText = String.format(Locale.ROOT, "%.1f", tps);
        NamedTextColor tpsColor = tps >= 19.5
                ? NamedTextColor.GREEN
                : tps >= 18.0 ? NamedTextColor.YELLOW : NamedTextColor.RED;
        int ping = Math.max(0, player.getPing());
        NamedTextColor pingColor = ping <= 100
                ? NamedTextColor.GREEN
                : ping <= 200 ? NamedTextColor.YELLOW : NamedTextColor.RED;
        Component header = Component.text("TPS ", NamedTextColor.GRAY)
                .append(Component.text(tpsText, tpsColor))
                .append(Component.text(" PING ", NamedTextColor.GRAY))
                .append(Component.text(Integer.toString(ping), pingColor));
        player.sendPlayerListHeader(header);
    }

    private void publishNotification(String eventType, String message) {
        AccountStore store = accountStore;
        if (store == null) {
            return;
        }
        String notification = message.length() > 3000
                ? message.substring(0, 3000) + "…"
                : message;
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            try {
                store.enqueueNotification(eventType, notification);
            } catch (SQLException exception) {
                getLogger().log(Level.WARNING, "Не удалось сохранить оповещение.", exception);
            }
        });
    }

    private void pollTelegramChat() {
        if (!telegramChatPollRunning.compareAndSet(false, true)) {
            return;
        }

        AccountStore store = accountStore;
        if (store == null) {
            telegramChatPollRunning.set(false);
            return;
        }

        try {
            List<TelegramChatMessage> messages = store.pendingTelegramChatMessages(100);
            if (messages.isEmpty()) {
                telegramChatPollRunning.set(false);
                return;
            }

            Bukkit.getScheduler().runTask(this, () -> {
                List<? extends Player> recipients = Bukkit.getOnlinePlayers().stream()
                        .filter(this::isAuthenticated)
                        .toList();
                if (recipients.isEmpty()) {
                    telegramChatPollRunning.set(false);
                    return;
                }

                List<Long> messageIds = messages.stream()
                        .map(TelegramChatMessage::id)
                        .toList();
                Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
                    try {
                        store.markTelegramChatMessagesDelivered(messageIds);
                        Bukkit.getScheduler().runTask(this, () -> {
                            for (TelegramChatMessage message : messages) {
                                Component chatLine = Component.text("TG", TELEGRAM_COLOR)
                                        .decorate(TextDecoration.BOLD)
                                        .append(Component.space()
                                                .decoration(TextDecoration.BOLD, false))
                                        .append(Component.text(
                                                        message.nickname(), NamedTextColor.WHITE)
                                                .decoration(TextDecoration.BOLD, false))
                                        .append(Component.text(": ", NamedTextColor.DARK_GRAY)
                                                .decoration(TextDecoration.BOLD, false))
                                        .append(Component.text(
                                                        message.message(), NamedTextColor.WHITE)
                                                .decoration(TextDecoration.BOLD, false));
                                for (Player recipient : recipients) {
                                    if (recipient.isOnline() && isAuthenticated(recipient)) {
                                        recipient.sendMessage(chatLine);
                                    }
                                }
                            }
                            telegramChatPollRunning.set(false);
                        });
                    } catch (SQLException exception) {
                        getLogger().log(Level.WARNING,
                                "Не удалось отметить сообщения Telegram доставленными.", exception);
                        telegramChatPollRunning.set(false);
                    }
                });
            });
        } catch (SQLException exception) {
            getLogger().log(Level.WARNING,
                    "Не удалось прочитать очередь чата Telegram.", exception);
            telegramChatPollRunning.set(false);
        }
    }

    private void pollTelegramPrivateMessages() {
        if (!telegramPrivateMessagePollRunning.compareAndSet(false, true)) {
            return;
        }

        AccountStore store = accountStore;
        if (store == null) {
            telegramPrivateMessagePollRunning.set(false);
            return;
        }

        try {
            List<TelegramPrivateMessage> messages = store.pendingTelegramPrivateMessages(100);
            if (messages.isEmpty()) {
                telegramPrivateMessagePollRunning.set(false);
                return;
            }

            Bukkit.getScheduler().runTask(this, () -> {
                List<Long> deliveredIds = new ArrayList<>();
                for (TelegramPrivateMessage message : messages) {
                    Player recipient = Bukkit.getOnlinePlayers().stream()
                            .filter(player -> player.getName()
                                    .equalsIgnoreCase(message.recipientNickname()))
                            .filter(this::isAuthenticated)
                            .findFirst()
                            .orElse(null);
                    if (recipient == null) {
                        continue;
                    }

                    Component privateLine = Component.text("TG", TELEGRAM_COLOR)
                            .decorate(TextDecoration.BOLD)
                            .append(Component.space()
                                    .decoration(TextDecoration.BOLD, false))
                            .append(Component.text(
                                            message.senderNickname(), NamedTextColor.WHITE)
                                    .decoration(TextDecoration.BOLD, false))
                            .append(Component.text(" → ты: ", NamedTextColor.DARK_GRAY)
                                    .decoration(TextDecoration.BOLD, false))
                            .append(Component.text(message.message(), NamedTextColor.WHITE)
                                    .decoration(TextDecoration.BOLD, false));
                    recipient.sendMessage(privateLine);
                    deliveredIds.add(message.id());
                }

                if (deliveredIds.isEmpty()) {
                    telegramPrivateMessagePollRunning.set(false);
                    return;
                }

                Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
                    try {
                        store.markPrivateMessagesDelivered(deliveredIds);
                    } catch (SQLException exception) {
                        getLogger().log(Level.WARNING,
                                "Не удалось отметить личные сообщения Telegram доставленными.",
                                exception);
                    } finally {
                        telegramPrivateMessagePollRunning.set(false);
                    }
                });
            });
        } catch (SQLException exception) {
            getLogger().log(Level.WARNING,
                    "Не удалось прочитать очередь личных сообщений Telegram.", exception);
            telegramPrivateMessagePollRunning.set(false);
        }
    }

    private void syncWhitelist() {
        AccountStore store = accountStore;
        if (store == null) {
            return;
        }
        try {
            for (String nickname : store.listAllowedNicknames()) {
                OfflinePlayer offlinePlayer = Bukkit.getOfflinePlayer(nickname);
                if (!offlinePlayer.isWhitelisted()) {
                    offlinePlayer.setWhitelisted(true);
                }
            }
        } catch (Exception exception) {
            getLogger().log(Level.SEVERE, "Не удалось обновить игровой белый список.", exception);
        }
    }
}
