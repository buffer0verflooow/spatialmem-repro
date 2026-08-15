package com.example.blindassist.p2p

/**
 * Wi-Fi Direct 链路的公共常量。手机（GO）与眼镜（客户端）必须完全一致。
 */
object P2pConstants {
    const val TAG = "P2pLink"

    /**
     * 手机端 P2P 设备名（尽力设置，供眼镜端 peer 发现按名字过滤）。
     * 眼镜端 DNS-SD 服务发现在真机上报 ERROR（RayNeo 栈不支持），
     * 因此身份识别改用 peer 列表里的 deviceName。
     */
    const val DEVICE_NAME = "LinkSee-Phone"

    /** 眼镜端在 peer 列表里匹配手机的名字片段。 */
    const val PEER_NAME_MARKER = "LinkSee"

    /** DNS-SD 注册类型（保留：部分设备可用，作为身份识别补充）。 */
    const val SERVICE_TYPE = "_linksee._tcp"

    /** DNS-SD 实例名，仅用于日志与过滤。 */
    const val SERVICE_INSTANCE = "LinkSee-Phone"

    /**
     * 凭据接力 UDP 端口：眼镜端向局域网单播扫网发 WHO，手机（GO）收到后
     * 单播回复 P2P 组 SSID/密码；眼镜端再以标准 Wi-Fi 方式加入 DIRECT 组
     * （绕开部分设备损坏的 P2P 发现 API 与被系统策略拦截的 UDP 广播）。
     */
    const val CREDENTIAL_PORT = 47811

    /** 凭据消息前缀：`LINKSEE_CRED <ssid>|<passphrase>|<tcpPort>`。 */
    const val CREDENTIAL_PREFIX = "LINKSEE_CRED "

    /** 眼镜端扫网询问前缀：`LINKSEE_WHO`。 */
    const val WHO_PREFIX = "LINKSEE_WHO"

    /** 链路 TCP 端口，与 GlassLinkServer.DEFAULT_PORT 一致。 */
    const val TCP_PORT = 47810

    /**
     * 标准 Android P2P group owner 地址（手机热点式组）。实际地址以
     * WifiP2pInfo.groupOwnerAddress 上报为准，这里只作兜底。
     */
    const val DEFAULT_GROUP_OWNER_IP = "192.168.49.1"
}
