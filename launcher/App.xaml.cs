using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using System.Windows;

namespace CyberpunkLauncher;

public partial class App : Application
{
    internal static readonly string GameRoot = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
        @"Steam\steamapps\common\Cyberpunk 2077");
    internal static readonly string Exe = Path.Combine(GameRoot, @"bin\x64\Cyberpunk2077.exe");
    internal static readonly string DataRoot = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CyberpunkAvatarBridge");
    internal static readonly string AvatarApp = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, @"..\AvatarBridge.exe"));

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Contains("--check") || e.Args.Contains("--launch"))
        {
            try
            {
                var issues = CheckFiles();
                string action = "checked";
                if (e.Args.Contains("--launch"))
                {
                    if (issues.Count > 0) throw new IOException(string.Join("\n", issues));
                    action = Launch(false);
                }
                var report = new { game = GameRoot, executable = Exe, issues, action,
                    running = FindGame() != null, utc = DateTimeOffset.UtcNow };
                Directory.CreateDirectory(DataRoot);
                File.WriteAllText(Path.Combine(DataRoot, "launcher-check.json"),
                    JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
                Shutdown(issues.Count == 0 ? 0 : 2);
            }
            catch (Exception error)
            {
                Directory.CreateDirectory(DataRoot);
                File.WriteAllText(Path.Combine(DataRoot, "launcher-check.json"),
                    JsonSerializer.Serialize(new { error = error.Message }));
                Shutdown(1);
            }
            return;
        }
        new MainWindow().Show();
    }

    internal static List<string> CheckFiles()
    {
        var issues = new List<string>();
        if (!File.Exists(Exe)) issues.Add("Eseguibile di Cyberpunk non trovato nella cartella del gioco.");
        foreach (var relative in new[] { @"bin\x64\version.dll", @"bin\x64\plugins\cyber_engine_tweaks.asi",
            @"bin\x64\plugins\AvatarBridge25.asi", @"archive\pc\mod\zz_avatarbridge.archive",
            @"bin\x64\plugins\cyber_engine_tweaks\mods\AvatarBridge\init.lua" })
        {
            if (!File.Exists(Path.Combine(GameRoot, relative)))
                issues.Add("File necessario all'avatar mancante: " + Path.GetFileName(relative));
        }
        // Verify installed script/native files against the transactional install
        // record. The large avatar archive is checked by the main installer.
        var recordPath = Path.Combine(DataRoot, @"installations\93bf13e663b2cc1c.json");
        if (File.Exists(recordPath))
        {
            using var record = JsonDocument.Parse(File.ReadAllText(recordPath));
            foreach (var entry in record.RootElement.GetProperty("files").EnumerateArray())
            {
                string path = entry.GetProperty("path").GetString()!;
                var expected = entry.GetProperty("sha256");
                if (expected.ValueKind != JsonValueKind.String || !File.Exists(path)
                    || (!path.EndsWith(".asi", StringComparison.OrdinalIgnoreCase)
                        && !path.EndsWith(".lua", StringComparison.OrdinalIgnoreCase))) continue;
                using var input = File.OpenRead(path);
                if (!Convert.ToHexString(SHA256.HashData(input)).Equals(expected.GetString(), StringComparison.OrdinalIgnoreCase))
                    issues.Add("File dell'avatar modificato: " + Path.GetFileName(path));
            }
        }
        return issues;
    }

    internal static Process? FindGame()
    {
        foreach (var process in Process.GetProcessesByName("Cyberpunk2077"))
        {
            if (!process.HasExited) return process;
            process.Dispose();
        }
        return null;
    }

    internal static string Launch(bool focus)
    {
        using var existing = FindGame();
        if (existing != null)
        {
            if (focus && existing.MainWindowHandle != IntPtr.Zero)
            {
                ShowWindowAsync(existing.MainWindowHandle, 9);
                SetForegroundWindow(existing.MainWindowHandle);
            }
            return "already_running";
        }
        // Launch the existing game normally. No Steam URLs, client process,
        // account changes, executable patches or licensing modifications.
        using var launched = Process.Start(new ProcessStartInfo(Exe)
        {
            WorkingDirectory = Path.GetDirectoryName(Exe)!,
            UseShellExecute = false
        });
        if (launched == null) throw new IOException("Windows non ha avviato Cyberpunk.");
        return "started";
    }

    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindowAsync(IntPtr window, int command);
}
