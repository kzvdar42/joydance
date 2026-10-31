from websockets.asyncio.client import ClientConnection


class JustDanceConnection(ClientConnection):
    """Use empty Pings because some Just Dance servers always send empty Pongs."""

    async def ping(self, data=b""):
        return await super().ping(data)
