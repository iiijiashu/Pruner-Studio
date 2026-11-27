package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.netease.NeteaseCrypto
import org.junit.Assert.*
import org.junit.Test

class NeteaseCryptoTest {
    private val json = """{"id":123,"title":"音"}"""

    @Test
    fun desktopEapiVector() {
        val expected =
            "4DC723619A991588865191FD2F319BAD3685FE9763EC6B583EF8EE54A9618FB734CD27EC9FA35D284D2DFE876D54B7EFAB11E129E4636C5884D647F6879992CB52653416C4F595011833E7C4EC116096C2F5D61EDFFD9DAE2E8BC0374E06936D"
        assertEquals(expected, NeteaseCrypto.eapiEncrypt("/api/test", json)["params"])
        assertEquals(
            "/api/test-36cd479b6b5-$json-36cd479b6b5-c17da79cae4463c4381b8f5c61627d48",
            NeteaseCrypto.eapiDecrypt(expected).decodeToString(),
        )
    }

    @Test
    fun desktopWeapiVector() {
        val result = NeteaseCrypto.weapiEncrypt(json, "0123456789abcdef")
        assertEquals(
            "STVwwMFOXmrpXLMGffyqsn2kCBSmEhh3puZ5W9RCuaWUZJhN60u672gn1qRwpTm4",
            result["params"],
        )
        assertEquals(
            "35701388baf89fed412e11269b9c76625d095ecaf17f03fa018abe19ea2d38b949debf242ee39a71ca1f6cda71b1b86a45aa909ee27f7e78e267d34e732f0de948206c3340a788d0003372183e2f753c1f78b66ac23d134ac1fc9b993156520ea826b8aa89a962d4491b4b8d7e08738e1da9b07aa39bf4a7ef0b1c210728cd52",
            result["encSecKey"],
        )
    }

    @Test
    fun skyPayloadAndCookiePrecedence() {
        val h = NeteaseCrypto.buildEapiHeader("deviceId=a; MUSIC_U=b; __csrf=c", "d", "r", "1")
        assertEquals("d", h["deviceId"])
        assertEquals("b", h["MUSIC_U"])
        assertTrue(NeteaseCrypto.buildEapiCookie("deviceId=a", h).endsWith("deviceId=a"))
        val p = NeteaseCrypto.buildPlayerUrlV1Payload(listOf(1, 2), "sky", header = h)
        assertEquals("\"[1, 2]\"", p["ids"].toString())
        assertEquals("\"c51\"", p["immerseType"].toString())
    }
}
