package net.pluresque.hub54auth;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.UUID;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

final class AccountStore {
    private final String jdbcUrl;

    AccountStore(Path databasePath) throws SQLException {
        if (!Files.isRegularFile(databasePath) || !Files.isReadable(databasePath)) {
            throw new IllegalStateException("Файл базы бота недоступен: " + databasePath);
        }
        this.jdbcUrl = "jdbc:sqlite:" + databasePath.toAbsolutePath();
        try (Connection connection = open(); Statement statement = connection.createStatement()) {
            statement.executeUpdate("""
                    CREATE TABLE IF NOT EXISTS minecraft_auth_sessions (
                        player_uuid TEXT PRIMARY KEY,
                        telegram_id INTEGER NOT NULL,
                        password_hash TEXT NOT NULL,
                        address_fingerprint TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        last_authenticated_at INTEGER NOT NULL
                    )
                    """);
            statement.executeUpdate("""
                    CREATE INDEX IF NOT EXISTS minecraft_auth_sessions_expiry
                    ON minecraft_auth_sessions(expires_at)
                    """);
            statement.executeUpdate("""
                    CREATE TABLE IF NOT EXISTS minecraft_private_message_outbox (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source TEXT NOT NULL CHECK(source IN ('minecraft', 'telegram')),
                        sender_telegram_id INTEGER NOT NULL,
                        sender_nickname TEXT NOT NULL,
                        recipient_telegram_id INTEGER NOT NULL,
                        recipient_nickname TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        delivered_at TEXT
                    )
                    """);
            statement.executeUpdate("""
                    CREATE INDEX IF NOT EXISTS minecraft_private_message_pending
                    ON minecraft_private_message_outbox(source, delivered_at, id)
                    """);
            statement.executeUpdate("""
                    CREATE TABLE IF NOT EXISTS minecraft_private_message_bridge (
                        bridge_name TEXT PRIMARY KEY,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """);
            try (ResultSet schemaCheck = statement.executeQuery(
                    "SELECT password_hash, last_login_at FROM profiles LIMIT 0")) {
                schemaCheck.getMetaData();
            }
        }
    }

    Optional<Account> findAllowedAccount(String nickname) throws SQLException {
        String sql = """
                SELECT p.telegram_id, p.nickname, p.password_hash
                FROM profiles p
                INNER JOIN whitelist w ON w.telegram_id = p.telegram_id
                WHERE p.nickname = ? COLLATE NOCASE
                LIMIT 2
                """;
        try (Connection connection = open();
             PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, nickname);
            try (ResultSet result = statement.executeQuery()) {
                if (!result.next()) {
                    return Optional.empty();
                }
                Account account = new Account(
                        result.getLong("telegram_id"),
                        result.getString("nickname"),
                        result.getString("password_hash")
                );
                if (result.next()) {
                    return Optional.empty();
                }
                return Optional.of(account);
            }
        }
    }

    List<String> listAllowedNicknames() throws SQLException {
        String sql = """
                SELECT p.nickname
                FROM profiles p
                INNER JOIN whitelist w ON w.telegram_id = p.telegram_id
                WHERE p.password_hash IS NOT NULL AND p.password_hash <> ''
                ORDER BY p.nickname COLLATE NOCASE
                """;
        List<String> nicknames = new ArrayList<>();
        try (Connection connection = open();
             PreparedStatement statement = connection.prepareStatement(sql);
             ResultSet result = statement.executeQuery()) {
            while (result.next()) {
                nicknames.add(result.getString("nickname"));
            }
        }
        return nicknames;
    }

    void recordSuccessfulLogin(
            long telegramId,
            UUID playerId,
            String addressFingerprint,
            long expiresAt
    ) throws SQLException {
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try (PreparedStatement profileUpdate = connection.prepareStatement(
                    "UPDATE profiles SET last_login_at = CURRENT_TIMESTAMP "
                            + "WHERE telegram_id = ?");
                 PreparedStatement sessionUpsert = connection.prepareStatement("""
                         INSERT INTO minecraft_auth_sessions (
                             player_uuid, telegram_id, password_hash,
                             address_fingerprint, expires_at, last_authenticated_at
                         )
                         SELECT ?, p.telegram_id, p.password_hash, ?, ?, ?
                         FROM profiles p
                         WHERE p.telegram_id = ?
                           AND p.password_hash IS NOT NULL
                           AND p.password_hash <> ''
                         ON CONFLICT(player_uuid) DO UPDATE SET
                             telegram_id = excluded.telegram_id,
                             password_hash = excluded.password_hash,
                             address_fingerprint = excluded.address_fingerprint,
                             expires_at = excluded.expires_at,
                             last_authenticated_at = excluded.last_authenticated_at
                         """)) {
                long now = System.currentTimeMillis() / 1000L;
                sessionUpsert.setString(1, playerId.toString());
                sessionUpsert.setString(2, addressFingerprint);
                sessionUpsert.setLong(3, expiresAt);
                sessionUpsert.setLong(4, now);
                sessionUpsert.setLong(5, telegramId);
                if (sessionUpsert.executeUpdate() != 1) {
                    throw new SQLException("Не удалось сохранить сессию авторизации.");
                }

                profileUpdate.setLong(1, telegramId);
                profileUpdate.executeUpdate();
                try (PreparedStatement cleanup = connection.prepareStatement(
                        "DELETE FROM minecraft_auth_sessions WHERE expires_at <= ?")) {
                    cleanup.setLong(1, now);
                    cleanup.executeUpdate();
                }
                connection.commit();
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    void recordSuccessfulLoginWithoutSession(long telegramId) throws SQLException {
        try (Connection connection = open();
             PreparedStatement statement = connection.prepareStatement(
                     "UPDATE profiles SET last_login_at = CURRENT_TIMESTAMP "
                             + "WHERE telegram_id = ? "
                             + "AND password_hash IS NOT NULL AND password_hash <> ''")) {
            statement.setLong(1, telegramId);
            if (statement.executeUpdate() != 1) {
                throw new SQLException("Не удалось обновить дату успешного входа.");
            }
        }
    }

    boolean resumeSession(
            UUID playerId,
            long telegramId,
            String passwordHash,
            String addressFingerprint,
            long now,
            long newExpiry
    ) throws SQLException {
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try (PreparedStatement sessionUpdate = connection.prepareStatement("""
                    UPDATE minecraft_auth_sessions
                    SET expires_at = ?, last_authenticated_at = ?
                    WHERE player_uuid = ?
                      AND telegram_id = ?
                      AND password_hash = ?
                      AND address_fingerprint = ?
                      AND expires_at > ?
                    """);
                 PreparedStatement profileUpdate = connection.prepareStatement(
                         "UPDATE profiles SET last_login_at = CURRENT_TIMESTAMP "
                                 + "WHERE telegram_id = ?")) {
                sessionUpdate.setLong(1, newExpiry);
                sessionUpdate.setLong(2, now);
                sessionUpdate.setString(3, playerId.toString());
                sessionUpdate.setLong(4, telegramId);
                sessionUpdate.setString(5, passwordHash);
                sessionUpdate.setString(6, addressFingerprint);
                sessionUpdate.setLong(7, now);
                if (sessionUpdate.executeUpdate() != 1) {
                    connection.rollback();
                    return false;
                }

                profileUpdate.setLong(1, telegramId);
                profileUpdate.executeUpdate();
                connection.commit();
                return true;
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    List<TelegramChatMessage> pendingTelegramChatMessages(int limit) throws SQLException {
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try {
                try (Statement statement = connection.createStatement()) {
                    statement.executeUpdate(
                            "DELETE FROM minecraft_chat_outbox "
                                    + "WHERE delivered_at IS NOT NULL "
                                    + "AND created_at < datetime('now', '-7 days')"
                    );
                    statement.executeUpdate(
                            """
                            UPDATE minecraft_chat_outbox
                            SET delivered_at = CURRENT_TIMESTAMP
                            WHERE delivered_at IS NULL AND (
                                created_at < datetime('now', '-1 minute')
                                OR NOT EXISTS (
                                    SELECT 1 FROM whitelist w
                                    INNER JOIN profiles p ON p.telegram_id = w.telegram_id
                                    WHERE w.telegram_id = minecraft_chat_outbox.telegram_id
                                      AND p.nickname = minecraft_chat_outbox.nickname COLLATE NOCASE
                                      AND p.nickname <> ''
                                )
                            )
                            """
                    );
                }

                List<TelegramChatMessage> messages = new ArrayList<>();
                try (PreparedStatement statement = connection.prepareStatement(
                        "SELECT id, nickname, message FROM minecraft_chat_outbox "
                                + "WHERE delivered_at IS NULL ORDER BY id LIMIT ?")) {
                    statement.setInt(1, limit);
                    try (ResultSet result = statement.executeQuery()) {
                        while (result.next()) {
                            messages.add(new TelegramChatMessage(
                                    result.getLong("id"),
                                    result.getString("nickname"),
                                    result.getString("message")
                            ));
                        }
                    }
                }
                connection.commit();
                return messages;
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    void markTelegramChatMessagesDelivered(List<Long> messageIds) throws SQLException {
        if (messageIds.isEmpty()) {
            return;
        }
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try (PreparedStatement statement = connection.prepareStatement(
                    "UPDATE minecraft_chat_outbox "
                            + "SET delivered_at = CURRENT_TIMESTAMP "
                            + "WHERE id = ? AND delivered_at IS NULL")) {
                for (long messageId : messageIds) {
                    statement.setLong(1, messageId);
                    statement.addBatch();
                }
                statement.executeBatch();
                connection.commit();
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    boolean enqueueMinecraftPrivateMessage(
            String senderNickname,
            String recipientNickname,
            String message
    ) throws SQLException {
        String sql = """
                INSERT INTO minecraft_private_message_outbox (
                    source, sender_telegram_id, sender_nickname,
                    recipient_telegram_id, recipient_nickname, message
                )
                SELECT 'minecraft', sender.telegram_id, sender.nickname,
                       recipient.telegram_id, recipient.nickname, ?
                FROM profiles sender
                INNER JOIN whitelist sender_whitelist
                    ON sender_whitelist.telegram_id = sender.telegram_id
                INNER JOIN profiles recipient
                    ON recipient.nickname = ? COLLATE NOCASE
                   AND recipient.nickname <> ''
                INNER JOIN whitelist recipient_whitelist
                    ON recipient_whitelist.telegram_id = recipient.telegram_id
                WHERE sender.nickname = ? COLLATE NOCASE
                  AND sender.nickname <> ''
                  AND sender.password_hash IS NOT NULL
                  AND sender.password_hash <> ''
                  AND recipient.password_hash IS NOT NULL
                  AND recipient.password_hash <> ''
                """;
        try (Connection connection = open();
             PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setString(1, message);
            statement.setString(2, recipientNickname);
            statement.setString(3, senderNickname);
            return statement.executeUpdate() == 1;
        }
    }

    void setPrivateMessageBridgeEnabled(boolean enabled) throws SQLException {
        try (Connection connection = open();
             PreparedStatement statement = connection.prepareStatement("""
                     INSERT INTO minecraft_private_message_bridge (
                         bridge_name, enabled, updated_at
                     ) VALUES ('private_messages', ?, CURRENT_TIMESTAMP)
                     ON CONFLICT(bridge_name) DO UPDATE SET
                         enabled = excluded.enabled,
                         updated_at = CURRENT_TIMESTAMP
                     """)) {
            statement.setInt(1, enabled ? 1 : 0);
            statement.executeUpdate();
        }
    }

    List<TelegramPrivateMessage> pendingTelegramPrivateMessages(int limit)
            throws SQLException {
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try {
                try (Statement cleanup = connection.createStatement()) {
                    cleanup.executeUpdate("""
                            UPDATE minecraft_private_message_outbox
                            SET delivered_at = CURRENT_TIMESTAMP
                            WHERE source = 'telegram' AND delivered_at IS NULL AND (
                                created_at < datetime('now', '-7 days')
                                OR NOT EXISTS (
                                    SELECT 1 FROM whitelist w
                                    INNER JOIN profiles p ON p.telegram_id = w.telegram_id
                                    WHERE w.telegram_id = recipient_telegram_id
                                      AND p.nickname = recipient_nickname COLLATE NOCASE
                                      AND p.password_hash IS NOT NULL
                                      AND p.password_hash <> ''
                                )
                                OR NOT EXISTS (
                                    SELECT 1 FROM whitelist w
                                    INNER JOIN profiles p ON p.telegram_id = w.telegram_id
                                    WHERE w.telegram_id = sender_telegram_id
                                      AND p.nickname = sender_nickname COLLATE NOCASE
                                      AND p.nickname <> ''
                                )
                            )
                            """);
                    cleanup.executeUpdate("""
                            DELETE FROM minecraft_private_message_outbox
                            WHERE delivered_at IS NOT NULL
                              AND created_at < datetime('now', '-7 days')
                            """);
                }

                List<TelegramPrivateMessage> messages = new ArrayList<>();
                try (PreparedStatement statement = connection.prepareStatement("""
                        SELECT id, sender_telegram_id, sender_nickname,
                               recipient_telegram_id, recipient_nickname, message
                        FROM minecraft_private_message_outbox
                        WHERE source = 'telegram' AND delivered_at IS NULL
                        ORDER BY id LIMIT ?
                        """)) {
                    statement.setInt(1, limit);
                    try (ResultSet result = statement.executeQuery()) {
                        while (result.next()) {
                            messages.add(new TelegramPrivateMessage(
                                    result.getLong("id"),
                                    result.getLong("sender_telegram_id"),
                                    result.getString("sender_nickname"),
                                    result.getLong("recipient_telegram_id"),
                                    result.getString("recipient_nickname"),
                                    result.getString("message")
                            ));
                        }
                    }
                }
                connection.commit();
                return messages;
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    void markPrivateMessagesDelivered(List<Long> messageIds) throws SQLException {
        if (messageIds.isEmpty()) {
            return;
        }
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try (PreparedStatement statement = connection.prepareStatement(
                    "UPDATE minecraft_private_message_outbox "
                            + "SET delivered_at = CURRENT_TIMESTAMP "
                            + "WHERE id = ? AND source = 'telegram' "
                            + "AND delivered_at IS NULL")) {
                for (long messageId : messageIds) {
                    statement.setLong(1, messageId);
                    statement.addBatch();
                }
                statement.executeBatch();
                connection.commit();
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    void enqueueNotification(String eventType, String message) throws SQLException {
        try (Connection connection = open()) {
            connection.setAutoCommit(false);
            try {
                try (PreparedStatement statement = connection.prepareStatement(
                        "SELECT 1 FROM notification_preferences "
                                + "WHERE event_type = ? AND enabled = 1 LIMIT 1")) {
                    statement.setString(1, eventType);
                    try (ResultSet result = statement.executeQuery()) {
                        if (!result.next()) {
                            connection.rollback();
                            return;
                        }
                    }
                }

                try (PreparedStatement statement = connection.prepareStatement(
                        "INSERT INTO minecraft_notification_events (event_type, message) "
                                + "VALUES (?, ?)")) {
                    statement.setString(1, eventType);
                    statement.setString(2, message);
                    statement.executeUpdate();
                }

                long notificationId;
                try (Statement statement = connection.createStatement();
                     ResultSet result = statement.executeQuery("SELECT last_insert_rowid()")) {
                    if (!result.next()) {
                        throw new SQLException("Не удалось получить ID оповещения.");
                    }
                    notificationId = result.getLong(1);
                }

                try (PreparedStatement statement = connection.prepareStatement(
                        """
                        INSERT INTO minecraft_notification_deliveries
                            (notification_id, telegram_id)
                        SELECT ?, telegram_id FROM notification_preferences
                        WHERE event_type = ? AND enabled = 1
                        """)) {
                    statement.setLong(1, notificationId);
                    statement.setString(2, eventType);
                    statement.executeUpdate();
                }

                try (Statement statement = connection.createStatement()) {
                    statement.executeUpdate(
                            """
                            DELETE FROM minecraft_notification_deliveries
                            WHERE notification_id IN (
                                SELECT id FROM minecraft_notification_events
                                WHERE created_at < datetime('now', '-7 days')
                            )
                            """
                    );
                    statement.executeUpdate(
                            "DELETE FROM minecraft_notification_events "
                                    + "WHERE created_at < datetime('now', '-7 days')"
                    );
                }
                connection.commit();
            } catch (SQLException exception) {
                connection.rollback();
                throw exception;
            }
        }
    }

    private Connection open() throws SQLException {
        Connection connection = DriverManager.getConnection(jdbcUrl);
        try (Statement statement = connection.createStatement()) {
            statement.execute("PRAGMA busy_timeout = 5000");
        }
        return connection;
    }
}
