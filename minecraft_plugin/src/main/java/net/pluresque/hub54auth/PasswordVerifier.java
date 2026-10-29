package net.pluresque.hub54auth;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Base64;

final class PasswordVerifier {
    private PasswordVerifier() {
    }

    static boolean verify(char[] password, String encoded) {
        if (encoded == null) {
            return false;
        }

        String[] parts = encoded.split("\\$", -1);
        if (parts.length != 4 || !parts[0].equals("pbkdf2_sha256")) {
            return false;
        }

        try {
            int iterations = Integer.parseInt(parts[1]);
            if (iterations < 100_000 || iterations > 2_000_000) {
                return false;
            }
            byte[] salt = Base64.getUrlDecoder().decode(parts[2]);
            byte[] expected = Base64.getUrlDecoder().decode(parts[3]);
            if (salt.length < 16 || expected.length != 32) {
                return false;
            }
            byte[] actual = derive(password, salt, iterations, expected.length);
            boolean matches = MessageDigest.isEqual(expected, actual);
            Arrays.fill(actual, (byte) 0);
            Arrays.fill(expected, (byte) 0);
            return matches;
        } catch (Exception ignored) {
            return false;
        }
    }

    private static byte[] derive(char[] password, byte[] salt, int iterations, int length)
            throws Exception {
        byte[] passwordBytes = new String(password).getBytes(StandardCharsets.UTF_8);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(passwordBytes, "HmacSHA256"));

        ByteArrayOutputStream firstInput = new ByteArrayOutputStream(salt.length + 4);
        firstInput.write(salt);
        firstInput.write(new byte[]{0, 0, 0, 1});
        byte[] current = mac.doFinal(firstInput.toByteArray());
        byte[] derived = current.clone();
        for (int index = 1; index < iterations; index++) {
            current = mac.doFinal(current);
            for (int byteIndex = 0; byteIndex < derived.length; byteIndex++) {
                derived[byteIndex] ^= current[byteIndex];
            }
        }

        Arrays.fill(passwordBytes, (byte) 0);
        Arrays.fill(current, (byte) 0);
        return Arrays.copyOf(derived, length);
    }
}
