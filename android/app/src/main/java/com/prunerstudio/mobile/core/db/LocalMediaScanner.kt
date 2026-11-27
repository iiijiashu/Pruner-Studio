package com.prunerstudio.mobile.core.db

import android.content.*
import android.net.Uri
import android.provider.MediaStore.Audio.Media
import androidx.core.net.toUri
import com.prunerstudio.mobile.core.model.AudioErrorCategory
import com.prunerstudio.mobile.core.model.AudioResolutionException
import com.prunerstudio.mobile.core.model.Track
import kotlinx.coroutines.*

private val ALBUM_ART_BASE_URI: Uri = "content://media/external/audio/albumart".toUri()

class LocalMediaScanner(private val context: Context, private val repository: PrunerRepository) {
    suspend fun scan(account: String) =
        withContext(Dispatchers.IO) {
            val columns =
                arrayOf(
                    Media._ID,
                    Media.TITLE,
                    Media.ARTIST,
                    Media.ALBUM,
                    Media.DURATION,
                    Media.ALBUM_ID,
                )
            val cursor =
                context.contentResolver.query(
                    Media.EXTERNAL_CONTENT_URI,
                    columns,
                    "${Media.IS_MUSIC}!=0",
                    null,
                    Media._ID,
                ) ?: throw AudioResolutionException(
                    AudioErrorCategory.DEVICE_FILE_ERROR,
                    "无法访问本机媒体库，请检查音频读取权限",
                )
            val tracks =
                cursor
                    .use { c ->
                        val albumIdCol = c.getColumnIndex(Media.ALBUM_ID)
                        buildList {
                            while (c.moveToNext()) {
                                val id = c.getLong(0)
                                val albumId = if (albumIdCol >= 0) c.getLong(albumIdCol) else 0L
                                val contentUri =
                                    ContentUris.withAppendedId(Media.EXTERNAL_CONTENT_URI, id)
                                val coverUri =
                                    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
                                        contentUri.toString()
                                    } else if (albumId > 0L) {
                                        ContentUris.withAppendedId(ALBUM_ART_BASE_URI, albumId).toString()
                                    } else {
                                        contentUri.toString()
                                    }
                                add(
                                    Track(
                                        provider = "device",
                                        external_id = id.toString(),
                                        track_key = "device:$id",
                                        title = c.getString(1).orEmpty(),
                                        artist = c.getString(2).orEmpty(),
                                        album = c.getString(3).orEmpty(),
                                        duration_ms = c.getLong(4),
                                        album_id = albumId.takeIf { it > 0L },
                                        cover_url = coverUri,
                                        stream_url = contentUri.toString(),
                                    )
                                )
                            }
                        }
                    }
            repository.indexDeviceTracks(account, tracks)
            tracks
        }
}
