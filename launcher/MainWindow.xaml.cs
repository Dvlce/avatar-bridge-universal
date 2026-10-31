using System.Diagnostics;
using System.IO;
using System.Windows;

namespace CyberpunkLauncher;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        Loaded += async (_, _) =>
        {
            try
            {
                var issues = await Task.Run(App.CheckFiles);
                if (issues.Count > 0)
                {
                    Status.Text = "L'avatar richiede un ripristino";
                    Detail.Text = string.Join("\n", issues) + "\nApri Gestisci avatar per reinstallare la correzione.";
                    return;
                }
                using var existing = App.FindGame();
                Status.Text = "Avatar pronto";
                Detail.Text = existing != null ? "Cyberpunk è già aperto. Puoi tornare alla partita." : "File presenti. Puoi avviare il gioco direttamente da qui.";
                Play.Content = existing != null ? "TORNA AL GIOCO" : "AVVIA CYBERPUNK";
                Play.IsEnabled = true;
            }
            catch (Exception error) { Status.Text = "Controllo non riuscito"; Detail.Text = error.Message; }
        };
    }

    private void PlayClick(object sender, RoutedEventArgs e)
    {
        Play.IsEnabled = false;
        try
        {
            var issues = App.CheckFiles();
            if (issues.Count > 0) throw new IOException(string.Join("\n", issues));
            App.Launch(true);
            Close();
        }
        catch (Exception error)
        {
            Status.Text = "Avvio non riuscito";
            Detail.Text = error.Message;
            Play.IsEnabled = true;
        }
    }

    private void AvatarClick(object sender, RoutedEventArgs e)
    {
        try { Process.Start(new ProcessStartInfo(App.AvatarApp) { UseShellExecute = true }); }
        catch (Exception error) { Detail.Text = error.Message; }
    }
}
