using System.Collections.Concurrent;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ChestCounter;

/// <summary>
/// Envío de cada partida contada al servidor del escuadrón (versión modificada para Tony y su amigo).
///
/// Se activa con un archivo <c>escuadron.json</c> junto a ChestCounter.exe o en la carpeta de datos
/// (%APPDATA%\ChestCounter):  { "url": "https://servidor", "clave": "..." }
/// Sin ese archivo el programa funciona igual que el original y no manda nada.
///
/// Lo que se manda es el registro de la partida que el programa ya guarda en data.json (id de la partida, cuenta,
/// cofres abiertos, cofres de los compañeros, tesoros). Nada más: ni repeticiones ni archivos.
/// </summary>
public static class EscuadronSync
{
    public const string ConfigFile = "escuadron.json";
    private const string StateFile = "escuadron-enviadas.json";
    private const int BatchSize = 25;

    private sealed class SyncConfig
    {
        [JsonPropertyName("url")] public string? Url { get; set; }
        [JsonPropertyName("clave")] public string? Clave { get; set; }
    }

    private static readonly JsonSerializerOptions Json = new()
    {
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private static string? _url;
    private static string? _clave;
    private static string _statePath = "";
    private static readonly object Gate = new();
    /// <summary>Firma de cada partida ya enviada (SessionId → firma de lo enviado: si cambia, se manda de nuevo).</summary>
    private static Dictionary<string, string> _sent = new();
    private static readonly ConcurrentQueue<(string Session, string Signature, string Json)> Pending = new();
    private static readonly HashSet<string> Queued = new();
    private static readonly AutoResetEvent Wake = new(false);
    private static bool _started;

    public static bool Enabled => _url != null && _clave != null;

    /// <summary>Texto de estado para la consola / ventana.</summary>
    public static string Status { get; private set; } = "";

    /// <summary>Lee escuadron.json y arranca el envío en segundo plano. No hace nada si el archivo no existe.</summary>
    public static void Start(string dataFolder, Func<bool> quitting)
    {
        if (_started)
        {
            return;
        }
        var candidates = new List<string>();
        if (AppInfo.ExePath is { } exe && Path.GetDirectoryName(exe) is { } exeDir)
        {
            candidates.Add(Path.Combine(exeDir, ConfigFile));
        }
        candidates.Add(Path.Combine(dataFolder, ConfigFile));
        var path = candidates.FirstOrDefault(File.Exists);
        if (path == null)
        {
            return;
        }
        try
        {
            var cfg = JsonSerializer.Deserialize<SyncConfig>(File.ReadAllText(path));
            var url = cfg?.Url?.Trim().TrimEnd('/');
            if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(cfg?.Clave)
                || !Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
            {
                Say(L.T("Escouade : escuadron.json invalide (url https et clave requises).", "Squad: invalid escuadron.json (https url and clave required).", "Escuadrón: escuadron.json no es válido (se necesitan url https y clave)."), ConsoleColor.Yellow);
                return;
            }
            _url = url;
            _clave = cfg!.Clave!.Trim();
        }
        catch (Exception ex)
        {
            Say($"Escuadrón: {ex.Message}", ConsoleColor.Yellow);
            return;
        }

        _statePath = Path.Combine(dataFolder, StateFile);
        try
        {
            if (File.Exists(_statePath))
            {
                _sent = JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(_statePath)) ?? new();
            }
        }
        catch
        {
            _sent = new();
        }

        _started = true;
        Status = L.T("Escouade : connexion…", "Squad: connecting…", "Escuadrón: conectando…");
        _ = Task.Run(() => Loop(quitting));
    }

    /// <summary>
    /// Pone en la cola las partidas nuevas o cambiadas. Se llama desde el ciclo principal (mismo hilo que modifica
    /// la lista), así que aquí se hace la copia en JSON y el envío ocurre en segundo plano.
    /// </summary>
    public static void Queue(IEnumerable<MatchRecord> matches)
    {
        if (!_started)
        {
            return;
        }
        var added = false;
        lock (Gate)
        {
            foreach (var m in matches)
            {
                if (string.IsNullOrEmpty(m.SessionId))
                {
                    continue;
                }
                var sig = Signature(m);
                if (_sent.TryGetValue(m.SessionId, out var done) && done == sig)
                {
                    continue;
                }
                if (!Queued.Add(m.SessionId + "|" + sig))
                {
                    continue;
                }
                Pending.Enqueue((m.SessionId, sig, JsonSerializer.Serialize(m, Json)));
                added = true;
            }
        }
        if (added)
        {
            Wake.Set();
        }
    }

    private static string Signature(MatchRecord m) =>
        $"{m.AnalyzerVersion}|{m.ChestsOpened}|{m.TeamChestsOpened}|{m.TeamChests.Count}|{m.TreasureFinds.Count}|{m.TeamTreasureFinds.Count}";

    private static async Task Loop(Func<bool> quitting)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
        http.DefaultRequestHeaders.UserAgent.ParseAdd($"ChestCounter-Escuadron/{AppInfo.VersionText}");
        http.DefaultRequestHeaders.Add("X-Clave", _clave);
        var delay = TimeSpan.FromSeconds(5);

        // Revisa la clave una vez al arrancar.
        try
        {
            using var ping = await http.GetAsync(_url + "/api/ping");
            if (ping.IsSuccessStatusCode)
            {
                var who = JsonDocument.Parse(await ping.Content.ReadAsStringAsync()).RootElement.GetProperty("jugador").GetString();
                Status = L.T($"Escouade : connecté ({who})", $"Squad: connected ({who})", $"Escuadrón: conectado ({who})");
                Say(Status, ConsoleColor.DarkCyan);
            }
            else if ((int)ping.StatusCode == 401)
            {
                Status = L.T("Escouade : clé refusée par le serveur", "Squad: key refused by the server", "Escuadrón: el servidor rechazó la clave");
                Say(Status, ConsoleColor.Yellow);
                return;
            }
        }
        catch
        {
            Status = L.T("Escouade : serveur injoignable, nouvel essai plus tard", "Squad: server unreachable, retrying later", "Escuadrón: no se pudo conectar, se reintenta más tarde");
        }

        while (!quitting())
        {
            if (Pending.IsEmpty)
            {
                Wake.WaitOne(TimeSpan.FromMinutes(1));
                continue;
            }
            var batch = new List<(string Session, string Signature, string Json)>();
            while (batch.Count < BatchSize && Pending.TryDequeue(out var item))
            {
                batch.Add(item);
            }
            try
            {
                var body = "[" + string.Join(",", batch.Select(b => b.Json)) + "]";
                using var content = new StringContent(body, Encoding.UTF8, "application/json");
                using var res = await http.PostAsync(_url + "/api/subir", content);
                if (!res.IsSuccessStatusCode)
                {
                    throw new HttpRequestException($"HTTP {(int)res.StatusCode}");
                }
                lock (Gate)
                {
                    foreach (var b in batch)
                    {
                        _sent[b.Session] = b.Signature;
                        Queued.Remove(b.Session + "|" + b.Signature);
                    }
                    SaveState();
                }
                Status = L.T($"Escouade : {batch.Count} partie(s) envoyée(s)", $"Squad: {batch.Count} match(es) sent", $"Escuadrón: {batch.Count} partida(s) enviada(s)");
                Say(Status, ConsoleColor.DarkCyan);
                delay = TimeSpan.FromSeconds(5);
            }
            catch (Exception ex)
            {
                // Se devuelven a la cola y se reintenta con espera creciente (máximo 10 minutos).
                foreach (var b in batch)
                {
                    Pending.Enqueue(b);
                }
                Status = L.T($"Escouade : échec de l'envoi ({ex.Message}), nouvel essai", $"Squad: send failed ({ex.Message}), retrying", $"Escuadrón: no se pudo enviar ({ex.Message}), se reintenta");
                await Task.Delay(delay);
                delay = TimeSpan.FromSeconds(Math.Min(600, delay.TotalSeconds * 2));
            }
        }
    }

    private static void SaveState()
    {
        try
        {
            var tmp = _statePath + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(_sent));
            File.Move(tmp, _statePath, overwrite: true);
        }
        catch { /* se vuelve a intentar en el siguiente envío */ }
    }

    private static void Say(string text, ConsoleColor color)
    {
        try
        {
            Console.ForegroundColor = color;
            Console.WriteLine($"{L.Stamp} {text}");
            Console.ResetColor();
        }
        catch { }
    }
}
