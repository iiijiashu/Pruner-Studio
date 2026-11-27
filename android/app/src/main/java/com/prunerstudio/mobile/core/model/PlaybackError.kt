package com.prunerstudio.mobile.core.model

import java.io.IOException

enum class AudioErrorCategory(val userMessage: String) {
    VIP_OR_TRIAL_ONLY("该曲目仅限 VIP 或试听，请登录会员账号后重试"),
    COPYRIGHT_OR_UNAVAILABLE("该曲目暂无可用音源，可能受版权限制"),
    LOGIN_REQUIRED("需要登录网易云音乐账号后才能播放"),
    RATE_LIMITED("触发风控或请求频率过高，已自动暂停写入，请稍后再试"),
    EXTERNAL_SOURCE_ERROR("外部音源暂不可用，请检查服务地址与凭据后重试"),
    PARSE_ERROR("音源响应解析失败，请稍后重试"),
    NETWORK_ERROR("网络连接异常，请检查网络后重试"),
    STREAM_EXPIRED("音频链接已失效，请点击播放重试"),
    LAN_UNPAIRED("桌面配对已断开，请先在设置中配对桌面"),
    LAN_UNREACHABLE("无法连接桌面端，请确认桌面已开启且在同一局域网"),
    DEVICE_FILE_ERROR("本机音频文件无法读取，可能已被移动或缺少权限"),
    CODEC_ERROR("音频格式解析或解码失败"),
    UNKNOWN("音频暂时无法播放，请稍后重试"),
}

class AudioResolutionException(
    val category: AudioErrorCategory,
    message: String = category.userMessage,
    cause: Throwable? = null,
) : IOException(message, cause)
