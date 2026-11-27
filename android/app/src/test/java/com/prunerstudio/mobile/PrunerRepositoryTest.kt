package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.db.*
import com.prunerstudio.mobile.core.lan.LanSyncClient
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.InMemoryCredentialStore
import java.security.MessageDigest
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.*
import okhttp3.mockwebserver.*
import okhttp3.tls.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class PrunerRepositoryTest {
    @Test
    fun localFeedbackSurvivesLegacyOutboxDrainAndPull() = runBlocking {
        val cert = HeldCertificate.Builder().commonName("desktop").build()
        val pin =
            MessageDigest.getInstance("SHA-256").digest(cert.certificate.encoded).joinToString("") {
                "%02x".format(it)
            }
        MockWebServer().use { server ->
            server.useHttps(
                HandshakeCertificates.Builder().heldCertificate(cert).build().sslSocketFactory(),
                false,
            )
            server.start()
            val account = "a"
            val config =
                PairedServerConfig(server.hostName, server.port, pin, "token", "d", account)
            val lan = LanSyncClient(config, InMemoryCredentialStore().apply { pairing = config })
            val helper = PrunerDatabaseHelper(RuntimeEnvironment.getApplication())
            try {
                val repo = PrunerRepository(helper)
                val db = helper.writableDatabase
                fun pending() =
                    db.rawQuery("SELECT COUNT(*) FROM pending_sync_outbox", null).use {
                        it.moveToFirst()
                        it.getInt(0)
                    }
                val local = FeedbackItem("replay", "device:1", client_event_id = "local")
                repo.recordFeedback(account, local)
                assertEquals(listOf(local), repo.feedback(account))
                assertEquals(0, pending())
                db.execSQL(
                    "INSERT INTO pending_sync_outbox VALUES(?,?,?,?)",
                    arrayOf(account, "feedback", "local", wire.encodeToString(local)),
                )
                repo.recordFeedback(
                    account,
                    FeedbackItem("replay", "netease:7", client_event_id = "valid"),
                )
                server.enqueue(
                    MockResponse()
                        .setBody(
                            """{"accepted_events":0,"accepted_feedback":1,"session_updated":false,"server_time":"now"}"""
                        )
                )
                val rules = listOf(
                    PruningRule(name = "absent"),
                    PruningRule(id = "__missing_id_0", name = "explicit", enabled = false),
                    PruningRule(id = "missing:0", name = "also explicit", action = "keep"),
                )
                val payload = """{"rules":${wire.encodeToString(rules)},"sync_until":"done","server_time":"now","account_id":"a","history_has_more":false,"feedback_has_more":false,"play_history":[],"feedback":[]}"""
                server.enqueue(MockResponse().setBody(payload))
                repo.syncWithDesktop(lan)
                val push = server.takeRequest()
                assertEquals("/api/lan/v1/sync/push", push.path)
                val sent =
                    wire
                        .parseToJsonElement(push.body.readUtf8())
                        .jsonObject
                        .getValue("feedback")
                        .jsonArray
                assertEquals(JsonPrimitive("valid"), sent.single().jsonObject["event_id"])
                assertEquals("/api/lan/v1/sync/pull", server.takeRequest().path)
                assertEquals(0, pending())
                assertTrue(local in repo.feedback(account))
                assertEquals("done", repo.watermark(account, pin).since)
                assertEquals(rules.toSet(), repo.rules(account).toSet())
                assertEquals(3, repo.rules(account).size)
                server.enqueue(MockResponse().setBody(payload.replace("absent", "obsolete").replace("done", "late")))
                val failure = runCatching {
                    repo.syncWithDesktop(lan, isStillActive = { false })
                }.exceptionOrNull()
                assertTrue(failure is kotlinx.coroutines.CancellationException)
                assertEquals(rules.toSet(), repo.rules(account).toSet())
                assertEquals("done", repo.watermark(account, pin).since)
            } finally {
                helper.close()
            }
        }
    }
}
