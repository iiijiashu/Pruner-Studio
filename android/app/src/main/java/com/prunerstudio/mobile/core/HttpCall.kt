package com.prunerstudio.mobile.core

import java.io.IOException
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.*

// Keep cancellation attached until the body is fully consumed, including slow streamed bodies.
internal suspend fun <T> Call.awaitResponse(read: (Response) -> T): T =
    suspendCancellableCoroutine { cont ->
        cont.invokeOnCancellation { cancel() }
        enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                cont.resumeWith(Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                cont.resumeWith(runCatching { response.use(read) })
            }
        })
    }
