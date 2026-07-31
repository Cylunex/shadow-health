package com.shadowverse.health;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class XiaomiScaleParserTest {
    private static final String ADDRESS = "8C:D0:B2:F6:BE:EF";
    private static final String BINDKEY = "0728974d657a4b60964c1b1677f35f7c";

    @Test
    public void parsesEncryptedWeightPacket() {
        byte[] raw = hex("4859d53b0abc078ff2348c844138e930220000009e538599");
        XiaomiScaleParser.S400Frame f = XiaomiScaleParser.parseS400(raw, ADDRESS, BINDKEY);
        assertNotNull(f);
        assertEquals(1, f.profileId);
        assertEquals(69.9, f.weightKg, 0.001);
        assertEquals(543.2, f.impedanceLow, 0.001);
        assertEquals(Integer.valueOf(92), f.heartRate);
        assertNull(f.impedanceHigh);
    }

    @Test
    public void parsesEncryptedHighFrequencyPacket() {
        byte[] raw = hex("4859d53b0bd6ef0b25db72785e7e2f46d6000000d8642df6");
        XiaomiScaleParser.S400Frame f = XiaomiScaleParser.parseS400(raw, ADDRESS, BINDKEY);
        assertNotNull(f);
        assertNull(f.weightKg);
        assertEquals(497.6, f.impedanceHigh, 0.001);
    }

    @Test
    public void rejectsMissingOrWrongKey() {
        byte[] raw = hex("4859d53b0abc078ff2348c844138e930220000009e538599");
        assertNull(XiaomiScaleParser.parseS400(raw, ADDRESS, ""));
        assertNull(XiaomiScaleParser.parseS400(raw, ADDRESS, "00000000000000000000000000000000"));
    }

    private static byte[] hex(String value) {
        byte[] out = new byte[value.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }
}
