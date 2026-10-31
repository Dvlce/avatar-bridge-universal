using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;
using Microsoft.Win32;
namespace AvatarBridge;

public partial class MainWindow : Window
{
    static readonly string DataRoot = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CyberpunkAvatarBridge");
    string? source;
    string? job;
    bool busy;
    bool selecting;
    bool selectingLibrary;
    bool hasRig;
    bool targetReady;
    string target = "gmod";
    string Target => target;
    public record LibraryItem(string Job, string Label) { public override string ToString() => Label; }
    Point? drag;
    double azimuth = 0, elevation = .08, distance = 4, radius = 1;
    readonly System.Windows.Threading.DispatcherTimer spin = new() { Interval = TimeSpan.FromMilliseconds(33) };
    Point3D center = new(0, 1, 0);
    public record Candidate(string Entry, long Size, string Format, string? Source = null)
    {
        public string Label => Size > 0 ? $"{Path.GetFileName(Entry)} · {Size / 1048576d:0.0} MB" : Path.GetFileName(Entry);
        public override string ToString() => Label;
    }
    public MainWindow()
    {
        InitializeComponent();
        spin.Tick += (_, _) => { azimuth += .014; Camera(); };
        Closed += (_, _) => spin.Stop();
        Loaded += async (_, _) =>
        {
            Directory.CreateDirectory(DataRoot);
            var savedTarget = Path.Combine(DataRoot, "target.txt");
            if (File.Exists(savedTarget) && new[]{"gmod","cyberpunk","portable"}.Contains(File.ReadAllText(savedTarget).Trim())) target = File.ReadAllText(savedTarget).Trim();
            TargetGame.SelectedIndex = target == "gmod" ? 0 : target == "cyberpunk" ? 1 : 2;
            targetReady = true;
            var cfg = TargetPathFile();
            GamePath.Text = DefaultGamePath();
            if (File.Exists(cfg)) GamePath.Text = File.ReadAllText(cfg);
            UpdateTargetControls();
            await CheckGame();
            var importSource=AppArgument("--open-source");
            if (importSource!=null) await Scan(importSource);
            if (importSource==null && !AppCommand("--smoke-test") && !AppCommand("--show-latest") && !AppCommand("--preview-360") && !AppCommand("--export-360")) await LoadLatest();
            if (AppCommand("--show-latest")) await LoadLatest();
            if (AppCommand("--preview-360")) { await LoadLatest(); ToggleSpin(); }
            if (AppCommand("--export-360"))
            {
                await LoadLatest();
                if (Viewport.Children.Count > 0) await Export360();
                Close();
            }
            if (!AppCommand("--smoke-test") && !AppCommand("--export-360")) await CheckAutomaticUpdate();
            if (AppCommand("--smoke-test"))
            {
                if(importSource==null) await LoadLatest();
                await Dispatcher.InvokeAsync(() => { }, System.Windows.Threading.DispatcherPriority.Render);
                var bitmap = new RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(this);
                var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using (var stream = File.Create(Path.Combine(DataRoot, "preview.png"))) encoder.Save(stream);
                File.WriteAllText(Path.Combine(DataRoot, "ui-smoke.json"), JsonSerializer.Serialize(new { status = Status.Text, runtime = RuntimeState.Text, stats = Stats.Text, models = Viewport.Children.Count, reportEnabled = ReportButton.IsEnabled, projectEnabled = ProjectButton.IsEnabled, job }));
                Close();
            }
        };
    }
    static bool AppCommand(string value) => Environment.GetCommandLineArgs().Contains(value);
    static string? AppArgument(string key) {
        var args=Environment.GetCommandLineArgs();var index=Array.IndexOf(args,key);
        return index>=0 && index+1<args.Length ? args[index+1] : null;
    }
    async Task<JsonElement> Engine(params string[] args)
    {
        var python = Path.Combine(AppContext.BaseDirectory,"runtime","python.exe");
        if (!File.Exists(python)) python = Path.Combine(DataRoot, "runtime", "Scripts", "python.exe");
        if (!File.Exists(python)) throw new InvalidOperationException("Manca il runtime locale. Avvia setup.ps1 nella cartella del progetto.");
        var script = Path.Combine(AppContext.BaseDirectory, "engine", "bridge.py");
        var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        start.ArgumentList.Add(script);
        foreach (var arg in args) start.ArgumentList.Add(arg);
        start.ArgumentList.Add("--target"); start.ArgumentList.Add(Target);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Impossibile avviare l'importatore.");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        var output = await stdout;
        var error = await stderr;
        if (string.IsNullOrWhiteSpace(output)) throw new InvalidOperationException(error.Length > 0 ? error : "L'importatore non ha restituito risultati.");
        using var doc = JsonDocument.Parse(output);
        if (!doc.RootElement.GetProperty("ok").GetBoolean()) throw new InvalidOperationException(doc.RootElement.GetProperty("error").GetString());
        return doc.RootElement.GetProperty("result").Clone();
    }
    void Busy(bool value, string text)
    {
        busy = value; Progress.Visibility = value ? Visibility.Visible : Visibility.Collapsed;
        Status.Text = text;
        PrepareButton.IsEnabled = !value && Candidates.SelectedItem is Candidate c && c.Format is ".asset" or ".glb" or ".vrm" or ".fbx" or ".blend";
        ProjectButton.IsEnabled = !value && job != null && hasRig;
        UninstallButton.IsEnabled = !value && Target != "portable";
        TargetGame.IsEnabled = !value;
        Candidates.IsEnabled = !value; Gender.IsEnabled = !value && Target == "cyberpunk"; GamePath.IsEnabled = !value; Lightweight.IsEnabled = !value;
        Library.IsEnabled = !value; AvatarName.IsEnabled = !value; AvatarCategory.IsEnabled = !value;
    }
    async Task CheckGame()
    {
        try
        {
            var result = await Engine("game", "--source", GamePath.Text);
            RuntimeState.Text = result.GetProperty("runtime").GetProperty("summary").GetString();
            if (Target != "cyberpunk") {
                GameState.Text = RuntimeState.Text;
                File.WriteAllText(TargetPathFile(), GamePath.Text);
                return;
            }
            int count = result.GetProperty("appearance_mods").GetArrayLength();
            GameState.Text = $"✓ Gioco trovato · {result.GetProperty("mods").GetArrayLength()} archivi mod" + (count > 0 ? $" · {count} mod di aspetto da considerare" : "");
            File.WriteAllText(TargetPathFile(), GamePath.Text);
        }
        catch (Exception ex) { GameState.Text = ex.Message; RuntimeState.Text = ex.Message; }
    }
    async Task Scan(string path)
    {
        if (busy) return;
        job = null;
        hasRig = false;
        Viewport.Children.Clear(); EmptyPreview.Visibility = Visibility.Visible;
        spin.Stop(); SpinButton.Content = "360°"; SpinButton.IsEnabled = false;
        Busy(true, "Leggo l'indice locale. Un archivio grande può richiedere qualche istante…");
        try
        {
            var result = await Engine("scan", "--source", path);
            source = path;
            SourceLabel.Text = Path.GetFileName(path);
            var candidates = result.GetProperty("candidates").EnumerateArray().Select(c => new Candidate(c.GetProperty("entry").GetString()!, c.GetProperty("size").GetInt64(), c.GetProperty("format").GetString()!, c.TryGetProperty("source",out var s) ? s.GetString() : path)).ToList();
            selecting = true;
            Candidates.ItemsSource = candidates;
            Candidates.SelectedIndex = candidates.Count > 0 ? 0 : -1;
            selecting = false;
            Busy(false, candidates.Count > 0 ? "Apro la base…" : "Nessun modello trovato.");
            if (candidates.Count > 0) await PrepareSelected();
        }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    async void BrowseSource(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        var dialog = new OpenFileDialog { Filter = "Avatar e archivi|*.rar;*.zip;*.7z;*.asset;*.glb;*.vrm;*.fbx;*.blend;*.obj|Tutti i file|*.*" };
        if (dialog.ShowDialog() == true) await Scan(dialog.FileName);
    }
    async void BrowseSourceFolder(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        var dialog = new OpenFolderDialog { Title = "Cartella dell'avatar" };
        if (dialog.ShowDialog() == true) await Scan(dialog.FolderName);
    }
    async void BrowseGame(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        var dialog = new OpenFolderDialog { Title = Target == "gmod" ? "Cartella principale di Garry's Mod" : "Cartella principale di Cyberpunk 2077" };
        if (dialog.ShowDialog() == true) { GamePath.Text = dialog.FolderName; await CheckGame(); }
    }
    void FilesDragOver(object sender, DragEventArgs e)
    {
        e.Effects = !busy && e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None; e.Handled = true;
    }
    async void FilesDropped(object sender, DragEventArgs e)
    {
        if (!busy && e.Data.GetData(DataFormats.FileDrop) is string[] files && files.Length > 0) await Scan(files[0]);
    }
    async void CandidateChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Candidates.SelectedItem is not Candidate c) return;
        CandidatePath.Text = c.Entry;
        PrepareButton.IsEnabled = !busy && c.Format is ".asset" or ".glb" or ".vrm" or ".fbx" or ".blend";
        if (!selecting && !busy && source != null) await PrepareSelected();
    }
    async void PrepareClick(object sender, RoutedEventArgs e)
        => await PrepareSelected();
    async Task PrepareSelected()
    {
        if (busy || source == null || Candidates.SelectedItem is not Candidate c) return;
        Busy(true, "Caricamento…");
        try
        {
            var args = new List<string> { "prepare", "--source", c.Source ?? source, "--entry", c.Entry, "--gender", Gender.SelectedIndex == 0 ? "female" : "male" };
            if (Lightweight.IsChecked != true) args.Add("--full");
            var result = await Engine(args.ToArray());
            await LoadReport(result);
            Busy(false, hasRig ? "Avatar caricato." : "Anteprima: il file non contiene uno scheletro.");
        }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    async Task LoadReport(JsonElement report)
    {
        job = report.GetProperty("job").GetString();
        AvatarName.Text = report.TryGetProperty("name",out var title) ? title.GetString() : "Avatar";
        AvatarCategory.Text = report.TryGetProperty("category",out var category) ? category.GetString() : "Avatar";
        source = report.GetProperty("source").GetString();
        SourceLabel.Text = source == null ? "" : Path.GetFileName(source);
        var entry = report.GetProperty("selected_base").GetString()!;
        var candidate = new Candidate(entry, 0, Path.GetExtension(entry).ToLowerInvariant());
        selecting = true;
        if (Candidates.ItemsSource is not IEnumerable<Candidate> available || !available.Any(c => c.Entry == entry))
            Candidates.ItemsSource = new[] { candidate };
        Candidates.SelectedItem = ((IEnumerable<Candidate>)Candidates.ItemsSource).First(c => c.Entry == entry);
        selecting = false;
        var file = report.GetProperty("output").GetString()!;
        if (!Path.GetFullPath(file).StartsWith(Path.GetFullPath(Path.Combine(DataRoot, "jobs")) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("L'export non si trova nella libreria locale.");
        var geometry = report.GetProperty("geometry");
        hasRig = geometry.TryGetProperty("bones",out var bones) && bones.GetInt32() > 0;
        Stats.Text = geometry.TryGetProperty("vertices", out var v) ? $"{v.GetInt32():N0} vertici · {geometry.GetProperty("triangles").GetInt32():N0} triangoli · {geometry.GetProperty("bones").GetInt32()} ossa" : "GLB della base importato";
        Gender.SelectedIndex = report.GetProperty("gender").GetString() == "male" ? 1 : 0;
        if (report.TryGetProperty("optimization", out var optimization) && optimization.ValueKind == JsonValueKind.Object)
        {
            int before = optimization.GetProperty("before").GetProperty("triangles").GetInt32();
            int after = optimization.GetProperty("after").GetProperty("triangles").GetInt32();
            Stats.Text += $" · −{100d * (before - after) / before:0}% triangoli";
        }
        Lightweight.IsChecked = report.TryGetProperty("optimization",out var optimizationState) && optimizationState.ValueKind == JsonValueKind.Object;
        Stage.Text = "Base avatar caricata";
        ReportButton.IsEnabled = OutputButton.IsEnabled = true;
        ProjectButton.IsEnabled = hasRig;
        SpinButton.IsEnabled = true;
        var model = await Task.Run(() => ModelReader.Load(file));
        Viewport.Children.Clear();
        var scene = new Model3DGroup();
        scene.Children.Add(new AmbientLight(Color.FromRgb(160, 160, 160)));
        scene.Children.Add(new DirectionalLight(Colors.White, new Vector3D(-1, -1, -2)));
        scene.Children.Add(model);
        Viewport.Children.Add(new ModelVisual3D { Content = scene });
        var b = model.Bounds;
        center = new Point3D(b.X + b.SizeX / 2, b.Y + b.SizeY / 2, b.Z + b.SizeZ / 2);
        radius = Math.Max(.01, Math.Max(b.SizeY, Math.Max(b.SizeX, b.SizeZ)));
        distance = radius * 2.5; azimuth = 0; elevation = .08; Camera(); EmptyPreview.Visibility = Visibility.Collapsed;
        RefreshLibrary();
        File.WriteAllText(Path.Combine(DataRoot,"selected-job.txt"),job!);
    }
    void RefreshLibrary()
    {
        var root = Path.Combine(DataRoot,"jobs");
        var items = new List<LibraryItem>();
        var identities = new HashSet<string>();
        foreach (var path in Directory.EnumerateFiles(root,"report.json",SearchOption.AllDirectories).OrderByDescending(File.GetLastWriteTimeUtc))
        {
            try {
                using var d = JsonDocument.Parse(File.ReadAllText(path)); var r=d.RootElement;
                var gender = r.GetProperty("gender").GetString();
                var identity = r.GetProperty("sha256").GetString()+gender;
                if (!identities.Add(identity)) continue;
                var title = r.TryGetProperty("name",out var n) ? n.GetString() : Path.GetFileName(r.GetProperty("selected_base").GetString());
                items.Add(new LibraryItem(Path.GetDirectoryName(path)!,title+(Target=="cyberpunk" ? " · "+(gender=="male" ? "V M" : "V F") : "")));
            } catch (JsonException) { }
        }
        selectingLibrary=true; Library.ItemsSource=items;
        Library.SelectedItem=items.FirstOrDefault(i=>i.Job==job);
        selectingLibrary=false;
    }
    async void LibraryChanged(object sender, SelectionChangedEventArgs e)
    {
        if (selectingLibrary || busy || Library.SelectedItem is not LibraryItem item) return;
        Busy(true,"Caricamento…");
        try {
            using var report=JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(item.Job,"report.json")));
            selecting=true;Candidates.ItemsSource=null;selecting=false;
            await LoadReport(report.RootElement);Busy(false,"Avatar caricato.");
        } catch (Exception ex) {Busy(false,ex.Message);}
    }
    async Task LoadLatest()
    {
        if (busy) return;
        var root = Path.Combine(DataRoot, "jobs");
        var latest = Directory.Exists(root) ? Directory.GetFiles(root, "report.json", SearchOption.AllDirectories).OrderByDescending(File.GetLastWriteTimeUtc).FirstOrDefault() : null;
        var remembered = Path.Combine(DataRoot,"selected-job.txt");
        if (File.Exists(remembered)) {
            var selected = Path.GetFullPath(File.ReadAllText(remembered).Trim());
            if (selected.StartsWith(Path.GetFullPath(root)+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase) && File.Exists(Path.Combine(selected,"report.json"))) latest = Path.Combine(selected,"report.json");
        }
        if (latest == null) { Status.Text = "La libreria è vuota. Importa una base per iniziare."; return; }
        Busy(true, "Apro l'ultimo modello locale…");
        try { using var doc = JsonDocument.Parse(await File.ReadAllTextAsync(latest)); await LoadReport(doc.RootElement); Busy(false, hasRig ? "Avatar caricato." : "Anteprima: il file non contiene uno scheletro."); }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    async void LatestClick(object sender, RoutedEventArgs e) => await LoadLatest();
    static void Open(string path) { if (File.Exists(path) || Directory.Exists(path)) Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
    void LibraryClick(object sender, RoutedEventArgs e) { var p = Path.Combine(DataRoot, "jobs"); Directory.CreateDirectory(p); Open(p); }
    void GameFolderClick(object sender, RoutedEventArgs e) => Open(GamePath.Text);
    void ReportClick(object sender, RoutedEventArgs e) { if (job != null) Open(Path.Combine(job, "report.json")); }
    void OutputClick(object sender, RoutedEventArgs e) { if (job != null) Open(job); }
    void ImportNav(object sender, RoutedEventArgs e) => DropZone.BringIntoView();
    async void ProjectClick(object sender, RoutedEventArgs e)
    {
        if (busy || job == null) return;
        Busy(true, "Preparo il progetto WolvenKit con le mesh di riferimento del gioco…");
        try
        {
            var result = await Engine("project", "--source", job, "--game", GamePath.Text);
            Busy(false, "Progetto preparato. Apri LEGGIMI.md: rig, materiali e divisione del modello richiedono ancora adattamento.");
            Open(result.GetProperty("project_dir").GetString()!);
        }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    void Camera()
    {
        var p = new Point3D(center.X + distance * Math.Sin(azimuth) * Math.Cos(elevation), center.Y + distance * Math.Sin(elevation), center.Z + distance * Math.Cos(azimuth) * Math.Cos(elevation));
        Viewport.Camera = new PerspectiveCamera(p, center - p, new Vector3D(0, 1, 0), 42) { NearPlaneDistance = radius / 1000, FarPlaneDistance = radius * 100 };
    }
    async void InstallClick(object sender, RoutedEventArgs e)
    {
        if (busy || job == null) return;
        Busy(true, "Preparazione mod…");
        try
        {
            if (Target != "cyberpunk")
            {
                var name = AvatarName.Text; var category = AvatarCategory.Text;
                using(var current = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(job,"report.json"))))
                {
                    var optimized = current.RootElement.TryGetProperty("optimization",out var optimization) && optimization.ValueKind==JsonValueKind.Object;
                    if(optimized != (Lightweight.IsChecked==true) && source!=null && Candidates.SelectedItem is Candidate choice)
                    {
                        var prepare = new List<string>{"prepare","--source",choice.Source ?? source,"--entry",choice.Entry,"--gender",current.RootElement.GetProperty("gender").GetString()!};
                        if(Lightweight.IsChecked!=true) prepare.Add("--full");
                        await LoadReport(await Engine(prepare.ToArray()));
                    }
                }
                AvatarName.Text=name; AvatarCategory.Text=category;
                await Engine("label","--source",job,"--name",AvatarName.Text,"--category",AvatarCategory.Text);
                var converted = await Engine("install","--source",job,"--game",GamePath.Text);
                if (Target == "portable") { Open(converted.GetProperty("output").GetString()!); Busy(false,"GLB esportato con scheletro e texture."); }
                else Busy(false,"Avatar installato in Garry's Mod. Selezionalo dal menu C > Modello giocatore e rinasci. I server devono avere lo stesso addon.");
                return;
            }
            var gameCheck = await Engine("game", "--source", GamePath.Text);
            var runtime = gameCheck.GetProperty("runtime");
            RuntimeState.Text = runtime.GetProperty("summary").GetString();
            if (!runtime.GetProperty("can_install").GetBoolean())
                throw new InvalidOperationException(RuntimeState.Text);
            var chosenName=AvatarName.Text;var chosenCategory=AvatarCategory.Text;
            using (var current = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(job,"report.json"))))
            {
                var selectedGender = Gender.SelectedIndex == 0 ? "female" : "male";
                var hasOptimization = current.RootElement.TryGetProperty("optimization",out var o) && o.ValueKind == JsonValueKind.Object;
                if ((current.RootElement.GetProperty("gender").GetString() != selectedGender || hasOptimization != (Lightweight.IsChecked == true) || (Candidates.SelectedItem is Candidate pick && pick.Entry != current.RootElement.GetProperty("selected_base").GetString())) && source != null && Candidates.SelectedItem is Candidate selected)
                {
                    var prepareArgs = new List<string>{"prepare","--source",selected.Source ?? source,"--entry",selected.Entry,"--gender",selectedGender};
                    if (Lightweight.IsChecked != true) prepareArgs.Add("--full");
                    var next = await Engine(prepareArgs.ToArray());
                    await LoadReport(next);
                }
            }
            AvatarName.Text=chosenName;AvatarCategory.Text=chosenCategory;
            await Engine("label","--source",job!,"--name",chosenName,"--category",chosenCategory);
            var buildPath = Path.Combine(job,"build.json");
            bool rebuild = !File.Exists(buildPath);
            if (!rebuild)
            {
                using var build = JsonDocument.Parse(await File.ReadAllTextAsync(buildPath));
                rebuild = !build.RootElement.TryGetProperty("builder_version",out var version) || version.GetInt32() < 24;
            }
            if (rebuild) await Engine("build", "--source", job, "--game", GamePath.Text);
            Status.Text = "Installazione…";
            await Engine("install", "--source", job, "--game", GamePath.Text);
            Busy(false, runtime.GetProperty("mode").GetString() == "compat25"
                ? "Avatar installato per Cyberpunk 2.5. Avvia il gioco. Per cambiarlo, torna qui a gioco chiuso."
                : "Mod installata. Avvia Cyberpunk.");
        }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    async void UninstallClick(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        Busy(true, "Rimozione…");
        try
        {
            await Engine("uninstall", "--source", job ?? DataRoot, "--game", GamePath.Text);
            Busy(false, "Mod rimossa.");
        }
        catch (Exception ex) { Busy(false, ex.Message); }
    }
    void PreviewDown(object sender, MouseButtonEventArgs e) { if (spin.IsEnabled) ToggleSpin(); drag = e.GetPosition(PreviewHost); PreviewHost.CaptureMouse(); }
    void PreviewUp(object sender, MouseButtonEventArgs e) { drag = null; PreviewHost.ReleaseMouseCapture(); }
    void PreviewMove(object sender, MouseEventArgs e)
    {
        if (drag is not Point old || e.LeftButton != MouseButtonState.Pressed) return;
        var now = e.GetPosition(PreviewHost); azimuth -= (now.X - old.X) * .01; elevation = Math.Clamp(elevation + (now.Y - old.Y) * .008, -1.3, 1.3); drag = now; Camera();
    }
    void PreviewWheel(object sender, MouseWheelEventArgs e) { distance = Math.Clamp(distance * (e.Delta > 0 ? .88 : 1.12), radius * .2, radius * 15); Camera(); }
    void ToggleSpin()
    {
        if (spin.IsEnabled) { spin.Stop(); SpinButton.Content = "▶ 360°"; }
        else { spin.Start(); SpinButton.Content = "Ⅱ Ferma"; }
    }
    void SpinClick(object sender, RoutedEventArgs e) => ToggleSpin();
    async Task Export360()
    {
        spin.Stop();
        var encoder = new GifBitmapEncoder();
        const int frames = 60;
        // Render with the application's actual WPF viewport, materials and lighting.
        for (int i = 0; i < frames; i++)
        {
            azimuth = 2 * Math.PI * i / frames; Camera();
            await Dispatcher.InvokeAsync(() => { }, System.Windows.Threading.DispatcherPriority.Render);
            await Task.Delay(25);
            var bitmap = new RenderTargetBitmap((int)PreviewHost.ActualWidth, (int)PreviewHost.ActualHeight, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(PreviewHost); bitmap.Freeze();
            var metadata = new BitmapMetadata("gif");
            metadata.SetQuery("/grctlext/Delay", (ushort)10);
            metadata.SetQuery("/grctlext/Disposal", (byte)2);
            encoder.Frames.Add(BitmapFrame.Create(bitmap, null, metadata, null));
        }
        using var memory = new MemoryStream(); encoder.Save(memory);
        var bytes = memory.ToArray();
        int table = (bytes[10] & 0x80) != 0 ? 3 * (1 << ((bytes[10] & 7) + 1)) : 0;
        int insert = 13 + table;
        // WPF's GIF encoder discards per-frame delay metadata. Write control blocks explicitly.
        int cursor = insert;
        void SkipBlocks() { int length; while ((length = bytes[cursor++]) != 0) cursor += length; }
        while (cursor < bytes.Length)
        {
            int marker = bytes[cursor++];
            if (marker == 0x3B) break;
            if (marker == 0x21)
            {
                int label = bytes[cursor++];
                if (label == 0xF9)
                {
                    bytes[cursor + 1] = (byte)((bytes[cursor + 1] & 0xE3) | 8);
                    bytes[cursor + 2] = 10; bytes[cursor + 3] = 0;
                }
                SkipBlocks();
            }
            else if (marker == 0x2C)
            {
                byte packed = bytes[cursor + 8]; cursor += 9;
                if ((packed & 0x80) != 0) cursor += 3 * (1 << ((packed & 7) + 1));
                cursor++; SkipBlocks();
            }
            else throw new InvalidOperationException("Blocco GIF inatteso.");
        }
        byte[] loop = { 0x21, 0xFF, 0x0B, 0x4E, 0x45, 0x54, 0x53, 0x43, 0x41, 0x50, 0x45, 0x32, 0x2E, 0x30, 0x03, 0x01, 0, 0, 0 };
        using var output = File.Create(Path.Combine(DataRoot, "avatar-360.gif"));
        output.Write(bytes, 0, insert); output.Write(loop); output.Write(bytes, insert, bytes.Length - insert);
    }
}
