package net.pluresque.hub54auth;

record TelegramPrivateMessage(
        long id,
        long senderTelegramId,
        String senderNickname,
        long recipientTelegramId,
        String recipientNickname,
        String message
) {
}
