using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace AvatarBridge;

public static class Updater
{
    public record Settings(string Repository = "", bool Automatic = true);
    public record Release(string Repository, string Version, string Zip, string Checksum);
    static readonly string Root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"CyberpunkAvatarBridge");
    static readonly string Config = Path.Combine(Root,"updates.json");
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMinutes(10) };
    static Updater() { Http.DefaultRequestHeaders.UserAgent.ParseAdd("AvatarBridge/0.7"); }

    public static string Normalize(string value)
    {
        value = value.Trim().TrimEnd('/');
        if (value.StartsWith("https://github.com/",StringComparison.OrdinalIgnoreCase)) value = value[19..];
        if (value.EndsWith(".git",StringComparison.OrdinalIgnoreCase)) value = value[..^4];
        if (!Regex.IsMatch(value,@"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$") || value.Split('/')[1] is "." or "..")
            throw new InvalidOperationException("Inserisci il repository come proprietario/nome oppure il link GitHub.");
        return value;
    }

    public static Settings Load()
    {
        if (File.Exists(Config)) return JsonSerializer.Deserialize<Settings>(File.ReadAllText(Config)) ?? new();
        var defaults = Path.Combine(AppContext.BaseDirectory,"distribution.json");
        return File.Exists(defaults) ? JsonSerializer.Deserialize<Settings>(File.ReadAllText(defaults)) ?? new() : new();
    }
    public static void Save(Settings settings)
    {
        Directory.CreateDirectory(Root);
        File.WriteAllText(Config,JsonSerializer.Serialize(settings,new JsonSerializerOptions{WriteIndented=true}));
    }

    public static async Task<Release?> Find(string repository)
    {
        repository = Normalize(repository);
        using var response = await Http.GetAsync("https://api.github.com/repos/"+repository+"/releases/latest");
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
            throw new InvalidOperationException("Repository pubblico o release non ancora disponibile su GitHub.");
        response.EnsureSuccessStatusCode();
        using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var release = doc.RootElement;
        var tag = release.GetProperty("tag_name").GetString()!.TrimStart('v');
        if (!Version.TryParse(tag,out var remote)) throw new InvalidOperationException("Versione release non valida.");
        var local = Assembly.GetExecutingAssembly().GetName().Version ?? new Version(0,0);
        // A release v0.7.0 is equal to assembly 0.7.0.0.
        if (new Version(remote.Major,remote.Minor,Math.Max(0,remote.Build),Math.Max(0,remote.Revision)) <= local) return null;
        string? zip=null, checksum=null;
        foreach (var asset in release.GetProperty("assets").EnumerateArray())
        {
            var name = asset.GetProperty("name").GetString();
            if (name == "AvatarBridge-win-x64.zip") zip = asset.GetProperty("browser_download_url").GetString();
            if (name == "AvatarBridge-win-x64.zip.sha256") checksum = asset.GetProperty("browser_download_url").GetString();
        }
        bool Valid(string? url) => Uri.TryCreate(url,UriKind.Absolute,out var uri) && uri.Scheme=="https" && uri.Host=="github.com" && uri.AbsolutePath.StartsWith("/"+repository+"/releases/download/",StringComparison.OrdinalIgnoreCase);
        if (!Valid(zip) || !Valid(checksum)) throw new InvalidOperationException("Pacchetto Windows o checksum assente nella release.");
        return new(repository,tag,zip!,checksum!);
    }

    public static async Task Install(Release release)
    {
        var work = Path.Combine(Root,"updates","github-"+Guid.NewGuid().ToString("N")); Directory.CreateDirectory(work);
        var archive = Path.Combine(work,"release.zip");
        using (var response = await Http.GetAsync(release.Zip,HttpCompletionOption.ResponseHeadersRead))
        {
            response.EnsureSuccessStatusCode();
            await using var file = File.Create(archive); await response.Content.CopyToAsync(file);
        }
        var expected = (await Http.GetStringAsync(release.Checksum)).Trim().Split((char[]?)null,StringSplitOptions.RemoveEmptyEntries)[0];
        await using (var file = File.OpenRead(archive))
        {
            var actual = Convert.ToHexString(await SHA256.HashDataAsync(file));
            if (!Regex.IsMatch(expected,"^[a-fA-F0-9]{64}$") || !actual.Equals(expected,StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Il checksum del pacchetto non corrisponde.");
        }
        var extracted = Path.Combine(work,"files"); Directory.CreateDirectory(extracted);
        using (var zip = ZipFile.OpenRead(archive))
        {
            if (zip.Entries.Sum(e=>e.Length)>2L*1024*1024*1024) throw new InvalidOperationException("Pacchetto troppo grande.");
            foreach (var entry in zip.Entries)
            {
                var destination = Path.GetFullPath(Path.Combine(extracted,entry.FullName));
                if (!destination.StartsWith(extracted+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase) || entry.FullName.Contains(':') || ((entry.ExternalAttributes>>16)&0xF000)==0xA000)
                    throw new InvalidOperationException("Il pacchetto contiene percorsi non validi.");
            }
        }
        ZipFile.ExtractToDirectory(archive,extracted);
        if (!File.Exists(Path.Combine(extracted,"AvatarBridge.exe")) || !File.Exists(Path.Combine(extracted,"engine","bridge.py")))
            throw new InvalidOperationException("Il pacchetto non contiene Avatar Bridge.");
        var target = Path.GetFullPath(AppContext.BaseDirectory).TrimEnd(Path.DirectorySeparatorChar);
        if (string.Equals(target,Path.GetPathRoot(target),StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Cartella dell'app non valida.");
        var payload = Path.Combine(work,"apply.json");
        File.WriteAllText(payload,JsonSerializer.Serialize(new { Process=Environment.ProcessId, Source=extracted, Target=target, Backup=Path.Combine(work,"backup"), Log=Path.Combine(work,"result.txt") }));
        var script = Path.Combine(work,"apply.ps1");
        File.WriteAllText(script,"""
param([string]$Payload)
$ErrorActionPreference = 'Stop'
$update = Get-Content -LiteralPath $Payload -Raw | ConvertFrom-Json
$targetRoot = [IO.Path]::GetFullPath($update.Target).TrimEnd('\')
$sourceRoot = [IO.Path]::GetFullPath($update.Source).TrimEnd('\')
if ($targetRoot -eq [IO.Path]::GetPathRoot($targetRoot).TrimEnd('\')) { throw 'Invalid application directory' }
Wait-Process -Id $update.Process -Timeout 60 -ErrorAction SilentlyContinue
if (Get-Process -Id $update.Process -ErrorAction SilentlyContinue) { throw 'Application is still running' }
New-Item -ItemType Directory -Path $update.Backup -Force | Out-Null
function Copy-UpdateFile([string]$SourceFile, [string]$DestinationFile) {
    for ($copyAttempt = 0; $copyAttempt -lt 40; $copyAttempt++) {
        try { Copy-Item -LiteralPath $SourceFile -Destination $DestinationFile -Force -ErrorAction Stop; return }
        catch { if ($copyAttempt -eq 39) { throw }; Start-Sleep -Milliseconds 250 }
    }
}
$changedFiles = [Collections.Generic.List[string]]::new()
try {
    foreach ($file in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse) {
        $relative = $file.FullName.Substring($sourceRoot.Length + 1)
        $destination = [IO.Path]::GetFullPath((Join-Path $targetRoot $relative))
        if (-not $destination.StartsWith($targetRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid update path' }
        $backup = Join-Path $update.Backup $relative
        if (Test-Path -LiteralPath $destination) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-UpdateFile $destination $backup
        }
        $changedFiles.Add($relative)
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-UpdateFile $file.FullName $destination
    }
    'OK' | Set-Content -LiteralPath $update.Log
} catch {
    $updateError = $_.Exception.Message
    foreach ($relative in $changedFiles) {
        $backup = Join-Path $update.Backup $relative
        $destination = Join-Path $targetRoot $relative
        try {
            if (Test-Path -LiteralPath $backup) { Copy-UpdateFile $backup $destination }
            elseif (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
        } catch { $updateError += "`nRollback: " + $_.Exception.Message }
    }
    $updateError | Set-Content -LiteralPath $update.Log
}
Start-Process -FilePath (Join-Path $targetRoot 'AvatarBridge.exe') -WorkingDirectory $targetRoot
""");
        var start = new ProcessStartInfo("powershell.exe") { UseShellExecute=false,CreateNoWindow=true };
        foreach(var argument in new[]{"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",script,"-Payload",payload}) start.ArgumentList.Add(argument);
        using var applying = Process.Start(start) ?? throw new InvalidOperationException("Impossibile applicare l'aggiornamento.");
    }
}

public sealed class UpdateWindow : Window
{
    public UpdateWindow()
    {
        Title="Aggiornamenti GitHub"; Width=510; Height=300; ResizeMode=ResizeMode.NoResize; WindowStartupLocation=WindowStartupLocation.CenterOwner;
        Background=new SolidColorBrush(Color.FromRgb(17,22,28)); Foreground=Brushes.White;
        var settings=Updater.Load(); var panel=new StackPanel{Margin=new Thickness(22)}; Content=panel;
        panel.Children.Add(new TextBlock{Text="Repository GitHub pubblico",Margin=new Thickness(0,0,0,8)});
        var repo=new TextBox{Text=settings.Repository,Margin=new Thickness(0,0,0,12)}; panel.Children.Add(repo);
        var automatic=new CheckBox{Content="Aggiorna automaticamente all'avvio",IsChecked=settings.Automatic,Foreground=Brushes.White,Margin=new Thickness(0,0,0,12)}; panel.Children.Add(automatic);
        var status=new TextBlock{Text="Gli avatar restano sul PC. GitHub distribuisce l'applicazione.",TextWrapping=TextWrapping.Wrap,Margin=new Thickness(0,0,0,12)};
        var button=new Button{Content="Salva e cerca aggiornamenti",Padding=new Thickness(8)}; panel.Children.Add(button); panel.Children.Add(status);
        button.Click+=async (_,_)=>{
            button.IsEnabled=false;
            try {
                var name=Updater.Normalize(repo.Text); Updater.Save(new(name,automatic.IsChecked==true));
                status.Text="Controllo GitHub…"; var release=await Updater.Find(name);
                if(release==null) status.Text="Hai già la versione più recente.";
                else { status.Text="Scarico la versione "+release.Version+"…"; await Updater.Install(release); Application.Current.Shutdown(); }
            } catch(Exception ex) { status.Text=ex.Message; }
            finally { button.IsEnabled=true; }
        };
        automatic.Unchecked+=(_,_)=>{ try { Updater.Save(Updater.Load() with {Automatic=false}); } catch { } };
    }
}
