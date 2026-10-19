# cython: language_level=3
"""MAC 通道 parser（0x12xx 命令族，自 pytdxdata 移植，2026-10-19 T0.4 实测门禁通过）。

MAC 通道与标准通道的差异（T0.4 实测结论）：
  - 同端口 7709，但只在 MAC 服务器池上可用（标准服务器对 MAC 帧无响应）；
  - 会话必须用老三条 SETUP 握手（新式 0x000D 握手会话下 MAC 帧超时无响应）；
  - 请求帧 10B 头（flag 0x1C）+ cmd u16 + body，与标准 12B 帧（0c 前缀）不同；
  - 响应帧头 16B（<IIIHH，magic 0x0074CBB1）与标准完全同构——BaseParser 收发逻辑复用。
"""
