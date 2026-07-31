package com.shadowverse.health;

import org.bouncycastle.crypto.InvalidCipherTextException;
import org.bouncycastle.crypto.engines.AESEngine;
import org.bouncycastle.crypto.modes.CCMBlockCipher;
import org.bouncycastle.crypto.modes.CCMModeCipher;
import org.bouncycastle.crypto.params.AEADParameters;
import org.bouncycastle.crypto.params.KeyParameter;

import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

/** 小米 S400（MJTZC01YM）MiBeacon v5 广播解析。 */
final class XiaomiScaleParser {
    private static final Set<Integer> S400_PRODUCT_IDS = new HashSet<>(
            Arrays.asList(0x30D9, 0x3BD5, 0x48CF));

    static final class S400Frame {
        final int profileId;
        final Double weightKg;
        final Double impedanceLow;
        final Double impedanceHigh;
        final Integer heartRate;
        final boolean reset;

        S400Frame(int profileId, Double weightKg, Double impedanceLow,
                  Double impedanceHigh, Integer heartRate, boolean reset) {
            this.profileId = profileId;
            this.weightKg = weightKg;
            this.impedanceLow = impedanceLow;
            this.impedanceHigh = impedanceHigh;
            this.heartRate = heartRate;
            this.reset = reset;
        }
    }

    private XiaomiScaleParser() {
    }

    static boolean isS400(byte[] data) {
        return data != null && data.length >= 5
                && S400_PRODUCT_IDS.contains(le16(data, 2));
    }

    static S400Frame parseS400(byte[] data, String address, String bindkeyHex) {
        if (!isS400(data) || data.length < 8) {
            return null;
        }
        int frameControl = le16(data, 0);
        if ((frameControl >>> 12) < 2 || (frameControl & 0x80) != 0
                || (frameControl & 0x40) == 0) {
            return null;
        }

        int offset = 5;
        byte[] xiaomiMac;
        if ((frameControl & 0x10) != 0) {
            if (data.length < offset + 6) {
                return null;
            }
            xiaomiMac = reverse(Arrays.copyOfRange(data, offset, offset + 6));
            offset += 6;
        } else {
            xiaomiMac = parseMac(address);
        }
        if (xiaomiMac == null) {
            return null;
        }
        if ((frameControl & 0x20) != 0) {
            if (data.length <= offset) {
                return null;
            }
            int capability = data[offset++] & 0xFF;
            if ((capability & 0x20) != 0) {
                offset++;
                if (data.length < offset) {
                    return null;
                }
            }
        }

        byte[] payload;
        if ((frameControl & 0x08) != 0) {
            byte[] key = parseHexKey(bindkeyHex);
            if (key == null || data.length < offset + 9) {
                return null;
            }
            byte[] nonce = concat(
                    reverse(xiaomiMac),
                    Arrays.copyOfRange(data, 2, 5),
                    Arrays.copyOfRange(data, data.length - 7, data.length - 4));
            byte[] ciphertextAndTag = concat(
                    Arrays.copyOfRange(data, offset, data.length - 7),
                    Arrays.copyOfRange(data, data.length - 4, data.length));
            payload = decryptCcm(key, nonce, ciphertextAndTag, new byte[]{0x11});
            if (payload == null) {
                return null;
            }
        } else {
            payload = Arrays.copyOfRange(data, offset, data.length);
        }

        for (int i = 0; i + 3 <= payload.length; ) {
            int type = le16(payload, i);
            int length = payload[i + 2] & 0xFF;
            int end = i + 3 + length;
            if (end > payload.length) {
                return null;
            }
            if (type == 0x6E16 && length == 9) {
                int p = i + 3;
                int profileId = payload[p] & 0xFF;
                long packed = le32(payload, p + 1);
                int mass = (int) (packed & 0x7FF);
                int heart = (int) ((packed >>> 11) & 0x7F);
                long impedance = packed >>> 18;
                if (mass == 0 && heart == 0 && impedance == 0) {
                    return new S400Frame(profileId, null, null, null, null, true);
                }
                Integer heartRate = heart > 0 && heart < 127 ? heart + 50 : null;
                Double z = impedance > 0 ? impedance / 10.0 : null;
                if (mass > 0) {
                    double weight = mass / 10.0;
                    if (weight < 10 || weight > 300) {
                        return null;
                    }
                    return new S400Frame(profileId, weight, z, null, heartRate, false);
                }
                if (heart == 0 && z != null) {
                    return new S400Frame(profileId, null, null, z, null, false);
                }
                return null;
            }
            i = end;
        }
        return null;
    }

    private static byte[] decryptCcm(byte[] key, byte[] nonce, byte[] input, byte[] aad) {
        try {
            CCMModeCipher cipher = CCMBlockCipher.newInstance(AESEngine.newInstance());
            cipher.init(false, new AEADParameters(new KeyParameter(key), 32, nonce, aad));
            byte[] out = new byte[cipher.getOutputSize(input.length)];
            int length = cipher.processBytes(input, 0, input.length, out, 0);
            length += cipher.doFinal(out, length);
            return Arrays.copyOf(out, length);
        } catch (InvalidCipherTextException | RuntimeException e) {
            return null;
        }
    }

    private static byte[] parseHexKey(String value) {
        if (value == null || !value.matches("(?i)[0-9a-f]{32}")) {
            return null;
        }
        byte[] out = new byte[16];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static byte[] parseMac(String address) {
        if (address == null) {
            return null;
        }
        String hex = address.replace(":", "").toUpperCase(Locale.US);
        if (!hex.matches("[0-9A-F]{12}")) {
            return null;
        }
        byte[] out = new byte[6];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static int le16(byte[] data, int offset) {
        return (data[offset] & 0xFF) | ((data[offset + 1] & 0xFF) << 8);
    }

    private static long le32(byte[] data, int offset) {
        return (data[offset] & 0xFFL)
                | ((data[offset + 1] & 0xFFL) << 8)
                | ((data[offset + 2] & 0xFFL) << 16)
                | ((data[offset + 3] & 0xFFL) << 24);
    }

    private static byte[] reverse(byte[] input) {
        byte[] out = input.clone();
        for (int i = 0; i < out.length / 2; i++) {
            byte tmp = out[i];
            out[i] = out[out.length - 1 - i];
            out[out.length - 1 - i] = tmp;
        }
        return out;
    }

    private static byte[] concat(byte[]... arrays) {
        int size = 0;
        for (byte[] a : arrays) {
            size += a.length;
        }
        byte[] out = new byte[size];
        int offset = 0;
        for (byte[] a : arrays) {
            System.arraycopy(a, 0, out, offset, a.length);
            offset += a.length;
        }
        return out;
    }
}
