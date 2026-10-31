using System.IO;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;

namespace AvatarBridge;

public partial class MainWindow
{
    string TargetPathFile() => Path.Combine(DataRoot, Target == "cyberpunk" ? "game-path.txt" : Target + "-path.txt");
    string DefaultGamePath()
    {
        if (Target == "portable") return DataRoot;
        var folder = Target == "gmod" ? "GarrysMod" : "Cyberpunk 2077";
        var steam = Registry.GetValue(@"HKEY_CURRENT_USER\Software\Valve\Steam", "SteamPath", null) as string;
        var libraries = new List<string>{steam ?? @"C:\Program Files (x86)\Steam"};
        var libraryFile = Path.Combine(libraries[0],"steamapps","libraryfolders.vdf");
        if (File.Exists(libraryFile))
            foreach (Match match in Regex.Matches(File.ReadAllText(libraryFile), "\"path\"\\s*\"([^\"]+)\""))
                libraries.Add(match.Groups[1].Value.Replace(@"\\",@"\"));
        return libraries.Select(p=>Path.Combine(p,"steamapps","common",folder)).FirstOrDefault(Directory.Exists)
            ?? Path.Combine(libraries[0],"steamapps","common",folder);
    }

    void UpdateTargetControls()
    {
        Gender.Visibility = Target == "cyberpunk" ? Visibility.Visible : Visibility.Collapsed;
        UninstallButton.IsEnabled = Target != "portable" && !busy;
        if (ProjectButton.Content is TextBlock label) label.Text = Target == "portable" ? "Esporta GLB" : "Installa avatar";
        Title = "Avatar Bridge 0.7 · " + (Target == "gmod" ? "Garry's Mod" : Target == "cyberpunk" ? "Cyberpunk 2077" : "Export universale");
    }

    async void TargetChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!targetReady || busy || TargetGame.SelectedItem is not ComboBoxItem selected) return;
        File.WriteAllText(TargetPathFile(),GamePath.Text);
        target = (string)selected.Tag;
        File.WriteAllText(Path.Combine(DataRoot,"target.txt"),target);
        GamePath.Text = File.Exists(TargetPathFile()) ? File.ReadAllText(TargetPathFile()) : DefaultGamePath();
        UpdateTargetControls();
        if (job != null) RefreshLibrary();
        await CheckGame();
    }

    async Task CheckAutomaticUpdate()
    {
        try
        {
            var settings = Updater.Load();
            if (!settings.Automatic || string.IsNullOrEmpty(settings.Repository)) return;
            var release = await Updater.Find(settings.Repository);
            if (release == null || busy) return;
            Busy(true,"Scarico Avatar Bridge " + release.Version + " da GitHub…");
            await Updater.Install(release);
            Application.Current.Shutdown();
        }
        catch (Exception ex) { Busy(false,"Aggiornamento non riuscito: "+ex.Message); }
    }

    void UpdatesClick(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        var window = new UpdateWindow { Owner=this };
        window.ShowDialog();
    }
}
