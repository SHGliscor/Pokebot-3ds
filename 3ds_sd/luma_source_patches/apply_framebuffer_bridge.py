from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
source_dir = root / "sysmodules" / "rosalina" / "source"
include_dir = root / "sysmodules" / "rosalina" / "include"

bridge_c = source_dir / "pokebot_ram_bridge.c"
draw_c = source_dir / "draw.c"
draw_h = include_dir / "draw.h"
menus_c = source_dir / "menus.c"

text = draw_h.read_text(encoding="utf-8")
prototype = (
    "void Draw_PokebotConvertFrameBufferSpan(u8 *buf, u32 width, u32 y, "
    "u32 xStart, u32 pixelCount, bool top, bool left);\n"
)
if "Draw_PokebotConvertFrameBufferSpan" not in text:
    marker = (
        "void Draw_ConvertFrameBufferLines(u8 *buf, u32 width, u32 startingLine, "
        "u32 numLines, u32 scaleFactorY, bool top, bool left);\n"
    )
    if marker not in text:
        raise SystemExit("draw.h framebuffer conversion marker not found")
    text = text.replace(marker, marker + prototype, 1)
    draw_h.write_text(text, encoding="utf-8")

text = draw_c.read_text(encoding="utf-8")
if "Draw_PokebotConvertFrameBufferSpan" not in text:
    if "Draw_ConvertFrameBufferLinesKernel" not in text or "Draw_ConvertPixelToBGR8" not in text:
        raise SystemExit("draw.c framebuffer conversion foundation not found")
    addition = r'''
/*
 * Pokebot read-only framebuffer span conversion.
 *
 * This is deliberately a converter only: no framebuffer address/register is
 * changed and no persistent screenshot buffer is allocated. The bridge asks
 * for bounded horizontal spans and the PC reconstructs the image.
 */
typedef struct PokebotFrameBufferSpanArgs
{
    u8 *buf;
    u32 width;
    u16 y;
    u16 xStart;
    u16 pixelCount;
    bool top;
    bool left;
} PokebotFrameBufferSpanArgs;

static void Draw_PokebotConvertFrameBufferSpanKernel(const PokebotFrameBufferSpanArgs *args)
{
    static const u8 formatSizes[] = { 4, 3, 2, 2, 2 };
    GSPGPU_FramebufferFormat fmt = args->top ?
        (GSPGPU_FramebufferFormat)(GPU_FB_TOP_FMT & 7) :
        (GSPGPU_FramebufferFormat)(GPU_FB_BOTTOM_FMT & 7);
    u32 stride = args->top ? GPU_FB_TOP_STRIDE : GPU_FB_BOTTOM_STRIDE;
    u32 pa = Draw_GetCurrentFramebufferAddress(args->top, args->left);
    u8 *addr = (u8 *)KERNPA2VA(pa);

    for (u32 i = 0; i < args->pixelCount; i++)
    {
        u32 x = (u32)args->xStart + i;
        const u8 *src = addr + x * stride + (u32)args->y * formatSizes[fmt];
        __builtin_prefetch(src, 0, 3);
        Draw_ConvertPixelToBGR8(args->buf + i * 3, src, fmt);
    }
}

void Draw_PokebotConvertFrameBufferSpan(
    u8 *buf,
    u32 width,
    u32 y,
    u32 xStart,
    u32 pixelCount,
    bool top,
    bool left)
{
    PokebotFrameBufferSpanArgs args = {
        buf,
        width,
        (u16)y,
        (u16)xStart,
        (u16)pixelCount,
        top,
        left
    };
    svcCustomBackdoor(Draw_PokebotConvertFrameBufferSpanKernel, &args);
}
'''
    text += addition
    draw_c.write_text(text, encoding="utf-8")

text = bridge_c.read_text(encoding="utf-8")

if '#include "draw.h"' not in text:
    marker = '#include "csvc.h"\n'
    if marker not in text:
        raise SystemExit("bridge include marker not found")
    text = text.replace(marker, marker + '#include "draw.h"\n', 1)

if "POKEBOT_MAX_PAYLOAD" not in text:
    marker = "#define POKEBOT_MAX_READ       0x200\n"
    if marker not in text:
        raise SystemExit("bridge max-read marker not found")
    text = text.replace(
        marker,
        marker
        + "#define POKEBOT_MAX_PAYLOAD    0x500\n"
        + "#define POKEBOT_FB_MAX_PIXELS   400\n",
        1,
    )

if "POKEBOT_CMD_FRAMEBUFFER_INFO" not in text:
    marker = "    POKEBOT_CMD_READ      = 4,\n"
    if marker not in text:
        raise SystemExit("bridge command enum marker not found")
    text = text.replace(
        marker,
        marker
        + "    POKEBOT_CMD_FRAMEBUFFER_INFO = 11,\n"
        + "    POKEBOT_CMD_FRAMEBUFFER_READ = 12,\n",
        1,
    )

if "POKEBOT_STATUS_FRAMEBUFFER_INVALID" not in text:
    marker = "    POKEBOT_STATUS_INTERNAL       = 11,\n"
    if marker not in text:
        raise SystemExit("bridge status enum marker not found")
    text = text.replace(
        marker,
        marker
        + "    POKEBOT_STATUS_FRAMEBUFFER_INVALID = 16,\n"
        + "    POKEBOT_STATUS_FRAMEBUFFER_UNSUPPORTED = 17,\n",
        1,
    )

if "PokebotFramebufferInfo" not in text:
    marker = r'''typedef struct PokebotQueryInfo
{
    u32 base;
    u32 size;
    u32 perm;
    u32 state;
    u32 pageFlags;
} PokebotQueryInfo;
'''
    if marker not in text:
        raise SystemExit("bridge query struct marker not found")
    addition = r'''
typedef struct PokebotFramebufferInfo
{
    u32 selector;
    u32 width;
    u32 height;
    u32 bytesPerPixel;
    u32 maxPixelsPerRead;
    u32 flags;
} PokebotFramebufferInfo;
'''
    text = text.replace(marker, marker + addition, 1)

if "Pokebot_GetFramebufferInfo" not in text:
    marker = "static void Pokebot_SendResponse(\n"
    if marker not in text:
        raise SystemExit("bridge send-response marker not found")
    helpers = r'''
#define POKEBOT_FB_TOP_LEFT   0UL
#define POKEBOT_FB_TOP_RIGHT  1UL
#define POKEBOT_FB_BOTTOM     2UL

#define POKEBOT_FB_FLAG_TOP        (1UL << 0)
#define POKEBOT_FB_FLAG_RIGHT_EYE  (1UL << 1)
#define POKEBOT_FB_FLAG_3D_ACTIVE  (1UL << 2)
#define POKEBOT_FB_FLAG_BGR8       (1UL << 3)
#define POKEBOT_FB_FLAG_LIVE       (1UL << 4)

static PokebotStatus Pokebot_GetFramebufferInfo(
    u32 selector,
    PokebotFramebufferInfo *out)
{
    if (selector > POKEBOT_FB_BOTTOM)
        return POKEBOT_STATUS_FRAMEBUFFER_INVALID;

    bool top = selector != POKEBOT_FB_BOTTOM;
    bool is3d = false;
    u32 width = 0;

    Draw_Lock();
    Draw_GetCurrentScreenInfo(&width, &is3d, top);
    Draw_Unlock();

    if (width == 0 || width > 800)
        return POKEBOT_STATUS_FRAMEBUFFER_UNSUPPORTED;

    memset(out, 0, sizeof(*out));
    out->selector = selector;
    out->width = width;
    out->height = 240;
    out->bytesPerPixel = 3;
    out->maxPixelsPerRead = POKEBOT_FB_MAX_PIXELS;
    out->flags = POKEBOT_FB_FLAG_BGR8 | POKEBOT_FB_FLAG_LIVE;
    if (top)
        out->flags |= POKEBOT_FB_FLAG_TOP;
    if (selector == POKEBOT_FB_TOP_RIGHT)
        out->flags |= POKEBOT_FB_FLAG_RIGHT_EYE;
    if (is3d)
        out->flags |= POKEBOT_FB_FLAG_3D_ACTIVE;

    return POKEBOT_STATUS_OK;
}

static PokebotStatus Pokebot_ReadFramebufferSpan(
    u32 argument,
    u32 pixelCount,
    u8 *out,
    u32 *outLength)
{
    u32 selector = argument & 0xFFUL;
    u32 y = (argument >> 8) & 0xFFUL;
    u32 xStart = (argument >> 16) & 0xFFFFUL;

    PokebotFramebufferInfo info;
    PokebotStatus status = Pokebot_GetFramebufferInfo(selector, &info);
    if (status != POKEBOT_STATUS_OK)
        return status;

    if (pixelCount == 0 || pixelCount > POKEBOT_FB_MAX_PIXELS)
        return POKEBOT_STATUS_LENGTH_INVALID;
    if (y >= info.height || xStart >= info.width ||
        (u64)xStart + (u64)pixelCount > (u64)info.width)
        return POKEBOT_STATUS_RANGE_INVALID;

    bool top = selector != POKEBOT_FB_BOTTOM;
    bool left = selector != POKEBOT_FB_TOP_RIGHT;

    Draw_Lock();
    Draw_PokebotConvertFrameBufferSpan(
        out, info.width, y, xStart, pixelCount, top, left);
    Draw_Unlock();

    *outLength = pixelCount * 3;
    return POKEBOT_STATUS_OK;
}

'''
    text = text.replace(marker, helpers + marker, 1)

old_buffer = "    u8 buffer[sizeof(PokebotResponse) + POKEBOT_MAX_READ + 32];\n"
new_buffer = "    u8 buffer[sizeof(PokebotResponse) + POKEBOT_MAX_PAYLOAD];\n"
if old_buffer in text:
    text = text.replace(old_buffer, new_buffer, 1)
elif new_buffer not in text:
    raise SystemExit("bridge response buffer marker not found")

if "req->command == POKEBOT_CMD_FRAMEBUFFER_INFO" not in text:
    marker = "    if (req->command == POKEBOT_CMD_GAME_INFO)\n"
    if marker not in text:
        raise SystemExit("bridge game-info route marker not found")
    route = r'''    if (req->command == POKEBOT_CMD_FRAMEBUFFER_INFO)
    {
        PokebotFramebufferInfo info;
        PokebotStatus status = Pokebot_GetFramebufferInfo(req->argument, &info);
        svcCloseHandle(target.process);
        Pokebot_SendResponse(
            sock, remote, remoteLen, req, status, 0,
            status == POKEBOT_STATUS_OK ? &info : NULL,
            status == POKEBOT_STATUS_OK ? sizeof(info) : 0);
        return;
    }

    if (req->command == POKEBOT_CMD_FRAMEBUFFER_READ)
    {
        u8 data[POKEBOT_FB_MAX_PIXELS * 3];
        u32 dataLength = 0;
        PokebotStatus status = Pokebot_ReadFramebufferSpan(
            req->argument, req->aux, data, &dataLength);
        svcCloseHandle(target.process);
        Pokebot_SendResponse(
            sock, remote, remoteLen, req, status, 0,
            status == POKEBOT_STATUS_OK ? data : NULL,
            status == POKEBOT_STATUS_OK ? dataLength : 0);
        return;
    }

'''
    text = text.replace(marker, route + marker, 1)

bridge_c.write_text(text, encoding="utf-8")

text = menus_c.read_text(encoding="utf-8")
if "Pokebot-Luma v0p5-fb1" not in text:
    if "Pokebot-Luma v0p5" not in text:
        raise SystemExit("v0p5 menu label not found")
    text = text.replace("Pokebot-Luma v0p5", "Pokebot-Luma v0p5-fb1")
    menus_c.write_text(text, encoding="utf-8")

print("Pokebot-Luma framebuffer extension applied.")
