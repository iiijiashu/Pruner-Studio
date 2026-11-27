package com.prunerstudio.mobile.core.db

import android.content.Context
import android.database.sqlite.*
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory

/**
 * Legacy-compatible SQLite helper kept for interoperability with existing tests and tools.
 * Production code uses [PrunerDatabase] (Room v2) with [PrunerMigrations.MIGRATION_1_2].
 */
class PrunerDatabaseHelper(context: Context, name: String = PrunerDatabase.DB_NAME) :
    SQLiteOpenHelper(context, name, null, 2) {
    internal val appContext: Context = context.applicationContext
    internal val dbName: String = name
    internal var attachedRoom: PrunerDatabase? = null

    init {
        setWriteAheadLoggingEnabled(true)
    }

    override fun onCreate(db: SQLiteDatabase) {
        // Create v1 tables then apply the exact Room v1->v2 migration so schema matches Room v2 bit-for-bit.
        for (table in
            listOf(
                "accounts",
                "playlists",
                "tracks",
                "play_history",
                "playback_session",
                "pruning_rules",
                "local_device_tracks",
                "lan_local_tracks",
                "sync_watermarks",
            )) db.execSQL(
            "CREATE TABLE $table(account_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,id))"
        )
        db.execSQL(
            "CREATE TABLE playlist_tracks(account_id TEXT NOT NULL, playlist_id TEXT NOT NULL, position INTEGER NOT NULL, track_key TEXT NOT NULL, PRIMARY KEY(account_id,playlist_id,position))"
        )
        db.execSQL(
            "CREATE TABLE play_events(event_id TEXT PRIMARY KEY NOT NULL, account_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        db.execSQL(
            "CREATE TABLE explicit_feedback(account_id TEXT NOT NULL, client_event_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,client_event_id))"
        )
        db.execSQL(
            "CREATE TABLE pending_sync_outbox(account_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,kind,id))"
        )
        upgradeViaRoomSql(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2 && newVersion >= 2) {
            upgradeViaRoomSql(db)
        }
    }

    private fun upgradeViaRoomSql(db: SQLiteDatabase) {
        // Bridge framework SQLiteDatabase through Room's single-source-of-truth migration logic.
        val supportDb = FrameworkSqlBridge(db)
        PrunerMigrations.migrateV1ToV2(supportDb)
    }

    override fun close() {
        attachedRoom?.close()
        attachedRoom = null
        super.close()
    }
}

/**
 * Minimal [androidx.sqlite.db.SupportSQLiteDatabase] adapter used exclusively so
 * [PrunerDatabaseHelper] shares the exact same DDL/backfill code as [PrunerMigrations.MIGRATION_1_2].
 */
private class FrameworkSqlBridge(private val db: SQLiteDatabase) :
    androidx.sqlite.db.SupportSQLiteDatabase {
    override fun execSQL(sql: String) = db.execSQL(sql)

    override fun execSQL(sql: String, bindArgs: Array<out Any?>) = db.execSQL(sql, bindArgs)

    override fun query(query: String) = db.rawQuery(query, null)

    override fun query(query: String, bindArgs: Array<out Any?>) =
        db.rawQuery(query, bindArgs.map { it?.toString() }.toTypedArray())

    override fun query(query: androidx.sqlite.db.SupportSQLiteQuery) = db.rawQuery(query.sql, null)

    override fun query(
        query: androidx.sqlite.db.SupportSQLiteQuery,
        cancellationSignal: android.os.CancellationSignal?,
    ) = db.rawQuery(query.sql, null, cancellationSignal)

    override fun compileStatement(sql: String) = throw UnsupportedOperationException()

    override fun beginTransaction() = db.beginTransaction()

    override fun beginTransactionNonExclusive() = db.beginTransactionNonExclusive()

    override fun beginTransactionWithListener(transactionListener: SQLiteTransactionListener) =
        db.beginTransactionWithListener(transactionListener)

    override fun beginTransactionWithListenerNonExclusive(
        transactionListener: SQLiteTransactionListener
    ) = db.beginTransactionWithListenerNonExclusive(transactionListener)

    override fun endTransaction() = db.endTransaction()

    override fun setTransactionSuccessful() = db.setTransactionSuccessful()

    override fun inTransaction() = db.inTransaction()

    override val isDbLockedByCurrentThread: Boolean
        get() = db.isDbLockedByCurrentThread()

    override fun yieldIfContendedSafely() = db.yieldIfContendedSafely()

    override fun yieldIfContendedSafely(sleepAfterYieldDelayMillis: Long) =
        db.yieldIfContendedSafely(sleepAfterYieldDelayMillis)

    override var version: Int
        get() = db.version
        set(value) {
            db.version = value
        }

    override val maximumSize: Long
        get() = db.maximumSize

    override fun setMaximumSize(numBytes: Long) = db.setMaximumSize(numBytes)

    override var pageSize: Long
        get() = db.pageSize
        set(value) {
            db.pageSize = value
        }

    override fun insert(table: String, conflictAlgorithm: Int, values: android.content.ContentValues) =
        db.insertWithOnConflict(table, null, values, conflictAlgorithm)

    override fun delete(table: String, whereClause: String?, whereArgs: Array<out Any?>?) =
        db.delete(table, whereClause, whereArgs?.map { it?.toString() }?.toTypedArray())

    override fun update(
        table: String,
        conflictAlgorithm: Int,
        values: android.content.ContentValues,
        whereClause: String?,
        whereArgs: Array<out Any?>?,
    ) =
        db.updateWithOnConflict(
            table,
            values,
            whereClause,
            whereArgs?.map { it?.toString() }?.toTypedArray(),
            conflictAlgorithm,
        )

    override fun needUpgrade(newVersion: Int) = db.needUpgrade(newVersion)

    override val path: String?
        get() = db.path

    override fun setLocale(locale: java.util.Locale) = db.setLocale(locale)

    override fun setMaxSqlCacheSize(cacheSize: Int) = db.setMaxSqlCacheSize(cacheSize)

    override fun setForeignKeyConstraintsEnabled(enabled: Boolean) =
        db.setForeignKeyConstraintsEnabled(enabled)

    override fun enableWriteAheadLogging() = db.enableWriteAheadLogging()

    override fun disableWriteAheadLogging() = db.disableWriteAheadLogging()

    override val isWriteAheadLoggingEnabled: Boolean
        get() = db.isWriteAheadLoggingEnabled

    override val attachedDbs: List<android.util.Pair<String, String>>?
        get() = db.attachedDbs

    override val isDatabaseIntegrityOk: Boolean
        get() = db.isDatabaseIntegrityOk

    override val isReadOnly: Boolean
        get() = db.isReadOnly

    override val isOpen: Boolean
        get() = db.isOpen

    override fun close() = db.close()
}
