from pathlib import Path

root = Path(__file__).resolve().parents[2] / "Luma3DS"
menus_path = root / "sysmodules" / "rosalina" / "source" / "menus.c"
text = menus_path.read_text(encoding="utf-8")

if "Pokebot-Luma v0p4" in text:
    raise SystemExit(0)

include_marker = '#include "luma_config.h"\n'
if include_marker not in text:
    raise SystemExit("include marker not found")

block = r'''
#include "input_redirection.h"
#include "pokebot_ram_bridge.h"

static Result sPokebotInputResult = 0;
static Result sPokebotRamResult = 0;

static Result PokebotBridge_SetInputEnabled(bool enable)
{
    if (enable)
    {
        if (inputRedirectionEnabled)
            return 0;

        s64 dummyInfo;
        bool isN3DS = svcGetSystemInfo(&dummyInfo, 0x10001, 0) == 0;
        bool isSocURegistered = false;
        Result res = srvIsServiceRegistered(&isSocURegistered, "soc:U");
        if (R_FAILED(res) || !isSocURegistered)
            return R_FAILED(res) ? res : (Result)-1;

        if (isN3DS)
        {
            bool isIrRstRegistered = false;
            res = srvIsServiceRegistered(&isIrRstRegistered, "ir:rst");
            if (R_FAILED(res) || !isIrRstRegistered)
                return R_FAILED(res) ? res : (Result)-2;
        }

        res = InputRedirection_DoOrUndoPatches();
        if (R_FAILED(res))
            return res;

        res = svcCreateEvent(&inputRedirectionThreadStartedEvent, RESET_STICKY);
        if (R_FAILED(res))
        {
            InputRedirection_DoOrUndoPatches();
            return res;
        }

        inputRedirectionCreateThread();
        res = svcWaitSynchronization(inputRedirectionThreadStartedEvent, 10 * 1000 * 1000 * 1000LL);
        if (res == 0)
            res = (Result)inputRedirectionStartResult;

        if (res != 0)
        {
            svcCloseHandle(inputRedirectionThreadStartedEvent);
            InputRedirection_DoOrUndoPatches();
            inputRedirectionEnabled = false;
        }
        inputRedirectionStartResult = 0;
        return res;
    }

    if (!inputRedirectionEnabled)
        return 0;

    return InputRedirection_Disable(5 * 1000 * 1000 * 1000LL);
}

static void PokebotBridge_ToggleRam(void)
{
    if (pokebotRamBridgeEnabled)
        sPokebotRamResult = PokebotRamBridge_Disable(5 * 1000 * 1000 * 1000LL);
    else
        sPokebotRamResult = PokebotRamBridge_Start();
}

static void PokebotBridge_ToggleInput(void)
{
    sPokebotInputResult = PokebotBridge_SetInputEnabled(!inputRedirectionEnabled);
}

static void PokebotBridge_EnableBoth(void)
{
    if (!pokebotRamBridgeEnabled)
        sPokebotRamResult = PokebotRamBridge_Start();
    if (!inputRedirectionEnabled)
        sPokebotInputResult = PokebotBridge_SetInputEnabled(true);
}

static void PokebotBridge_DisableBoth(void)
{
    if (pokebotRamBridgeEnabled)
        sPokebotRamResult = PokebotRamBridge_Disable(5 * 1000 * 1000 * 1000LL);
    if (inputRedirectionEnabled)
        sPokebotInputResult = PokebotBridge_SetInputEnabled(false);
}

static void PokebotBridge_ShowStatus(void)
{
    Draw_Lock();
    Draw_ClearFramebuffer();
    Draw_FlushFramebuffer();
    Draw_Unlock();

    do
    {
        Draw_Lock();
        Draw_DrawString(10, 10, COLOR_TITLE, "Pokebot3DS Bridge");
        u32 posY = 30;
        posY = Draw_DrawFormattedString(10, posY, COLOR_WHITE,
            "Pokebot-Luma v0p4\n\n"
            "RAM Bridge:       %s\n"
            "RAM result:       0x%08lx\n"
            "RAM UDP:          4952\n"
            "Packets / Reads:  %lu / %lu\n\n"
            "Input Controller: %s\n"
            "Input result:     0x%08lx\n"
            "Input UDP:        4950\n\n",
            pokebotRamBridgeEnabled ? "ON" : "OFF",
            (u32)sPokebotRamResult,
            pokebotRamBridgePackets,
            pokebotRamBridgeReads,
            inputRedirectionEnabled ? "ON" : "OFF",
            (u32)sPokebotInputResult);
        Draw_DrawString(10, posY, COLOR_WHITE,
            "RAM: read-only QUERY/READ, max 0x200.\n"
            "Controller: v0p3 additive path unchanged.\n\n"
            "Press B to go back.");
        Draw_FlushFramebuffer();
        Draw_Unlock();
    }
    while(!(waitInput() & KEY_B) && !menuShouldExit);
}

Menu pokebotBridgeMenu = {
    "Pokebot3DS Bridge",
    {
        { "Toggle RAM Bridge", METHOD, .method = &PokebotBridge_ToggleRam },
        { "Toggle Input Controller", METHOD, .method = &PokebotBridge_ToggleInput },
        { "Enable Both", METHOD, .method = &PokebotBridge_EnableBoth },
        { "Disable Both", METHOD, .method = &PokebotBridge_DisableBoth },
        { "Status", METHOD, .method = &PokebotBridge_ShowStatus },
        {},
    }
};
'''

text = text.replace(include_marker, include_marker + block, 1)
marker = '        { "Miscellaneous options...", MENU, .menu = &miscellaneousMenu },\n'
if marker not in text:
    raise SystemExit("menu marker not found")
text = text.replace(marker, marker + '        { "Pokebot3DS Bridge...", MENU, .menu = &pokebotBridgeMenu },\n', 1)
menus_path.write_text(text, encoding="utf-8")
