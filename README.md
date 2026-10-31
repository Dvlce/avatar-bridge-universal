# Avatar Bridge

## Versione universale 0.7.1

Apri **Avatar Bridge - Preview 360** sul desktop. Scegli **Garry's Mod** in alto,
carica Xeno e premi **Installa avatar** a gioco chiuso. Nel gioco apri il menu C,
seleziona il modello e rinasci. In una nuova partita locale l'addon seleziona
automaticamente l'avatar installato. In multiplayer server e giocatori devono
avere lo stesso addon; il server decide quali modelli consente.

La libreria sorgente e l'anteprima 360 sono condivise fra gli adattatori:

- **Garry's Mod**: conversione locale GLB → SMD/QC → StudioMDL → addon,
  rig ValveBiped, animazioni standard, texture VTF/VMT e ragdoll.
  Richiede una base umanoide riconosciuta e Garry's Mod installato da Steam.
  La coda segue il bacino, senza fisica dedicata; il volto rimane neutro.
  Le mani in prima persona sono quelle standard del gioco. Il risultato
  estetico e le animazioni richiedono una verifica per ogni modello.
- **Cyberpunk 2077**: conserva la conversione precedente e i suoi requisiti.
- **GLB universale (export)**: esporta scheletro e texture incorporati.
  Ogni altro gioco richiede un adattatore dedicato.

I giochi futuri si aggiungono in `engine/targets.py`. Percorsi, installazioni
e rimozioni sono separati per gioco. I file modificati manualmente vengono
conservati. Gli aggiornamenti dell'app non reinstallano automaticamente gli
avatar nei giochi.

**Aggiornamenti** salva il repository pubblico GitHub e l'opzione automatica.
All'avvio l'app cerca una nuova release stabile, scarica il pacchetto Windows,
verifica SHA-256, conserva un backup, applica la versione e si riapre.
Si pubblica il programma; avatar, texture personali e risorse estratte dai
giochi rimangono sul PC. La release comprende .NET, Python, dipendenze,
gltfpack e 7-Zip. FBX/BLEND richiedono Blender locale.

Per una nuova versione aggiorna `<Version>` in `src/AvatarBridge.csproj` e
invia il tag corrispondente, per esempio `v0.7.1`. Il workflow
`.github/workflows/release.yml` esegue i test e pubblica ZIP e checksum.
Puoi anche avviare il workflow manualmente indicando il tag.
Build locale: `./build-release.ps1 -Repository proprietario/nome`.

Fonti: [compilazione playermodel](https://wiki.facepunch.com/gmod/Player_model_compilation),
[animazioni GMod](https://wiki.facepunch.com/gmod/Player_Animations),
[selettore modelli](https://wiki.facepunch.com/gmod/player_manager.AddValidModel),
[scheletro Source](https://github.com/ValveSoftware/source-sdk-2013/blob/master/src/public/studio.h).

Le sezioni successive descrivono l'adattatore Cyberpunk originale.

## Avvio diretto dal desktop

Apri **Avvia Cyberpunk 2077** sul desktop, poi premi **AVVIA CYBERPUNK**.
L'app in `App/Launcher/AvviaCyberpunk.exe` avvia direttamente l'eseguibile del
gioco installato con la sua cartella di lavoro, senza comandi o collegamenti
Steam. Se il gioco e' gia' aperto, il pulsante riporta alla partita.
Prima dell'avvio controlla i file essenziali dell'avatar e verifica lo script e
il runtime installati. **Gestisci avatar** apre Avatar Bridge per reinstallarli.
Il launcher non modifica l'eseguibile del gioco, i salvataggi o le licenze.
Sorgenti in `launcher/`; compilazione: `dotnet publish launcher/CyberpunkLauncher.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o App/Launcher`.

Riparazione avvio del 23 ottobre: ripristinati i file ufficiali CET mancanti.
Il runtime dell'avatar aspetta ora le mesh caricate e i proxy di rendering pronti,
e non accoda un secondo caricamento del volto mentre il precedente e' in corso.
Le anteprime vengono registrate sull'evento OnPuppetReady, senza richiamare il
metodo GetPuppet dal proprio observer.

## Compatibilit? Cyberpunk 2.5 ? Avatar Bridge 0.6.3

La versione 0.6.3 corregge il collegamento della pelle alla radice della coda:
conserva i pesi della colonna e unifica quelli dei duplicati sul bordo fra corpo
e coda. Il profilo del cavallo usa una deformazione continua anche sui vertici
legati alle cosce; i pantaloni vengono riadattati alla nuova forma. Con i capi
inferiori si nascondono il piccolo cappuccio pubico e le sole sezioni di gamba
completamente racchiuse dal tessuto. Vita, addome e schiena rimangono visibili.
La copertura si aggiorna sulle anteprime dell'inventario e
non ricarica una mesh se la sua maschera non e' cambiata.

La versione 0.6.2 conserva tutte le sezioni della pelle anche con i vestiti,
riadatta i pantaloni includendo il bacino completo e nasconde le mesh dedicate
alle ombre dei vestiti umani. La verifica visiva di ogni posa resta necessaria.

La versione 0.6.1 accorcia il profilo anteriore del bacino senza eliminare
triangoli. La pelle del bacino resta completa anche con i pantaloni, evitando
buchi alla cintura. Le giacche esterne aperte, compresa quella di David Martinez,
non nascondono intere sezioni della pancia. Le correzioni vengono applicate alle
nuove compilazioni; una base precedente viene ricompilata quando premi Installa.

La modalit? compatibilit? genera sul PC una sostituzione completa delle risorse
native del giocatore e include un piccolo componente dedicato alla 2.5. Conserva
telecamera, dati di gioco, pelle completa, coda e i vestiti gi? adattati.
Gestisce la copertura del corpo e nasconde le scarpe per mostrare le zampe.
Il modello dell'inventario usa i propri vestiti visibili: togliere i pantaloni
ripristina la pelle senza dipendere dai vestiti del giocatore nel mondo.

Apri `App/AvatarBridge.exe`, seleziona l'avatar e premi **Installa** a gioco chiuso.
Sulla 2.5 viene installato un solo avatar alla volta: per cambiarlo, chiudi il
gioco e ripeti l'installazione dall'app. L'overlay CET conserva i controlli del
volto quando il modello li contiene; non sincronizza la bocca con i dialoghi.

Questa modalit? si abilita solo sull'eseguibile 2.5 riconosciuto e con CET presente,
RED4ext disattivato. Il programma conserva una copia di `scripts.ini` e sospende
redscript durante l'installazione; **Rimuovi mod** lo ripristina. Non riattiva le altre mod che dipendono da quei
framework. I vecchi ArchiveXL 1.27.5 e RED4ext 1.30.0 non sono compatibili con
questa installazione: [ArchiveXL #30](https://github.com/psiberx/cp2077-archive-xl/issues/30),
[RED4ext #153](https://github.com/wopss/RED4ext/issues/153).
Il pacchetto locale non va distribuito: contiene riferimenti ricompilati dai
file del gioco. I sorgenti del componente e le licenze sono in `native/`.

La correzione dorsale conserva la pelle anche quando le cuciture UV separano la superficie posteriore da quella anteriore. Le sezioni posteriori mancanti vengono interpolate fra sezioni reali sopra e sotto, senza estrapolare o cancellare triangoli. Il fit del pelo non usa la superficie anteriore come se fosse la schiena.

Applicazione Windows locale per importare la base Rexouium di un progetto VRChat e generare una sostituzione di V in Cyberpunk 2077 2.31 e 2.5. I file dell'avatar restano sul PC. Non viene caricata alcuna risorsa su GitHub o su altri servizi.

## Uso

Apri `App/AvatarBridge.exe`. L'ultimo avatar si apre automaticamente.

1. Seleziona o trascina la sorgente. Per i RAR divisi scegli `part1.rar`.
2. La prima base viene caricata automaticamente. Cambiando modello si aggiorna la preview; **Carica** forza la ricarica. Scegli il rig di V. **Leggero** riduce i triangoli.
3. Premi **Installa** a gioco chiuso. Il programma compila se necessario e installa i propri file.
4. **Rimuovi mod** rimuove i file installati e ripristina l'eventuale sostituzione di V precedentemente disattivata.

**360°** avvia/ferma la rotazione. Trascina il modello per ruotarlo e usa la rotella per lo zoom.

La lista in alto riapre gli avatar locali. I due campi sotto **Leggero** sono nome e categoria. Durante l'installazione viene assemblata una libreria con tutte le basi già compilate. Apri l'overlay di Cyber Engine Tweaks (su questo PC **F10**) per vedere **Avatar**, con sezioni per categoria e selezione del modello. La scelta resta sul PC in `selection.json`. Le basi devono essere compilate prima di avviare il gioco. Il rig maschile/femminile deve corrispondere al V del salvataggio.

La conversione leggera riduce i triangoli mantenendo la pelle originale. Corpo e testa usano il rig di V; la coda ha una catena fisica separata di sette ossa. La testa non viene aggiunta alla visuale in prima persona. Gli occhi usano geometria e albedo rossa originali. La lente speculare trasparente di Unity viene omessa per non coprire l'iride con una superficie opaca.

Le scarpe equipaggiate restano negli slot del gioco, ma le loro mesh vengono nascoste sul giocatore e nelle anteprime dell'inventario. Il corpo dell'avatar conserva le proprie zampe. La cresta dorsale separata viene rimossa solo dal materiale dei ciuffi. Le forme originali `Floof_*_Flat`, quando presenti, appiattiscono i ciuffi integrati senza eliminare triangoli: anche piccole isole separate dalle cuciture UV possono essere pelle del collo o del torace.

La base giocabile appiattisce inoltre le sporgenze del dorso contro sezioni continue del busto e ne collega i pesi alla colonna vertebrale, con transizione verso il collo. La superficie principale del torso e tutti gli indici della pelle vengono conservati. Questo evita di gonfiare le maglie attorno ai ciuffi o di tagliare la pelle per nasconderli. Se una nuova base non offre un riferimento anatomico sufficientemente chiaro, la correzione viene saltata. L'anteprima dell'app mostra la base sorgente; l'adattamento nativo viene verificato nel gioco.

La transizione della nuca usa anche la superficie integra del collo, per terminare sopra il colletto. Il refit riserva inoltre spazio fra pantaloni e capi esterni su vita e sedere. Questo spazio rimane nei morph Garment Support delle giacche; una maglia interna lunga conserva invece la forma compressa per infilarsi nei pantaloni. Gli orli lontani dal corpo e le aperture dei cappotti vengono conservati. Non equivale a una simulazione di collisioni fra ogni coppia di abiti.

Il corpo completo viene conservato. Le sue sezioni anatomiche sono nascoste temporaneamente sotto i vestiti in base alla copertura misurata sulla geometria del capo, nella stessa posa nativa del corpo. Le maschere includono corpo e braccia in prima persona e si aggiornano cambiando vestiti. La zona centrale anteriore del bacino viene nascosta con abbigliamento inferiore; mani, zampe e collo restano visibili. Le sezioni vicino agli orli conservano sovrapposizione dietro il tessuto. Si tratta di una copertura per sezioni: capi con aperture o tagli particolari possono richiedere adattamenti specifici.

Quando la base contiene forme facciali riconosciute, nel selettore F10 compaiono le espressioni disponibili. Sul Rexouium fornito: **Neutro**, **Sorriso**, **Arrabbiato**, **Triste**, **Sorpreso**, con chiusura automatica delle palpebre. Le forme provengono dalla base originale dell'avatar; vengono compilate localmente. Questo controllo non sincronizza la bocca con i dialoghi di V e non sostituisce i cursori delle espressioni della modalità foto.

## Cosa è implementato

- Lettura selettiva di RAR/ZIP/7z, anche multipart, e cartelle Unity, senza eseguire script dell'avatar.
- Importazione di mesh Unity YAML `.asset` con `.meta` e prefab; lettura di GLB/VRM incorporati.
- Anteprima locale con texture, rotazione e zoom.
- Riduzione del rig, decimazione con conservazione dei bordi, trasferimento di posa/pesi al rig di V e generazione di tangenti.
- Separazione corpo/testa, conversione delle texture in XBM e generazione di materiali REDengine.
- Mesh delle braccia separata per la prima persona, con il piano di rendering delle armi; torso e gambe mantengono il rendering del corpo.
- Collegamento dei ciuffi ai pesi della pelle vicina e generazione di rig/animgraph nativi per la coda.
- Refit locale dei capi base del gioco su torso, bacino, braccia e gambe. Le copie adattate conservano materiali e rig del capo, comprese le ossa delle parti lunghe. Il tessuto vicino alla pelle segue gradualmente i pesi del corpo; i pannelli lontani mantengono i propri pesi. Codeware ricarica la mesh sul personaggio selezionato.
- I pesi sono limitati e normalizzati alle quattro/otto influenze del formato prima di calcolare il binding. Questo evita che il tessuto cambi posizione quando il gioco legge i pesi esportati. Le verifiche native confrontano anche arti piegati e Garment Support compresso.
- Normalizzazione della posa di riferimento del capo sul rig completo del giocatore prima del refit; conversione inversa prima dell'importazione, per conservare i binding nativi.
- Volumi continui attorno a torso e braccia: il refit può allargare o restringere un capo mantenendo il lato del braccio a cui appartengono i vertici. La regolarizzazione dell'involucro conserva le sporgenze locali della pelle. Le parti libere lontane dal corpo conservano la loro forma. I margini geometrici sono 18 mm per le maglie, 25 mm per i pantaloni e 24 mm per le giacche, con agio limitato per conservare il taglio del capo.
- L'involucro del torso usa la superficie principale della pelle quando è identificabile per area e copertura anatomica. I duplicati creati dalle sezioni vengono riuniti usando posizione e UV, distinguendo i ciuffi sovrapposti che gonfiavano la schiena della maglia. Nessun triangolo del corpo viene cancellato. Se la superficie è incompleta o ambigua, il metodo conserva l'involucro completo.
- Garment Support ricostruito sulla nuova topologia: vertici base e morph usano lo stesso ordine e la stessa suddivisione. Anche le forme compresse vengono vincolate all'involucro del corpo, per evitare che una maglia adattata torni al suo interno quando si sovrappongono altri capi. La compilazione abilita l'importazione dei supporti, ripristinando poi le impostazioni di WolvenKit.
- Per pantaloni e pantaloncini, involucro continuo di bacino e cosce. Per le scarpe viene adattata anche l'impronta di suola e tomaia alle zampe. La deformazione viene regolarizzata alle cuciture, senza modificare le UV originali.
- Creazione di mesh ed entità, verifica tramite riesportazione di WolvenKit e compilazione dell'archivio.
- Installazione/rimozione con backup, controllo dei file modificati e rollback su errore.

La compilazione e l'installazione sono verificate separatamente dall'effettiva resa nel gioco. `build.json` non dichiara `runtime_verified` finché non esiste una verifica specifica del personaggio in gioco.

## Limiti

Il percorso automatico è stato costruito per la base Rexouium fornita. Non è un convertitore universale di qualunque avatar: usa nomi e gerarchia umanoidi riconosciuti; si ferma se mancano ossa o texture necessarie. Altri modelli possono richiedere adattamento del mapping.

Le espressioni selezionabili e il battito delle palpebre richiedono morph riconosciuti nella base; se mancano, il modello mantiene il volto neutro. Non sono implementate sincronizzazione dei dialoghi, animazione indipendente della lingua o fisica di baffi e ciuffi. Gli occhi restano solidali alla testa. La coda richiede almeno due ossa anatomiche riconosciute; catene con nomi diversi richiedono un mapping. Il refit usa la geometria del singolo avatar e conserva il rig del capo e i pesi delle parti libere, adattando quelli del tessuto a contatto con la pelle, con margine maggiore per giacche e pantaloni e suddivisione limitata dei triangoli più grandi. Sulla base maschile di prova sono compilate 325 mesh dei capi del gioco. Gli abiti aggiunti da altre mod non sono inclusi nel catalogo. La compatibilità con ogni posa, materiale, cyberware o combinazione di strati richiede una prova specifica. Il refit non è una simulazione del tessuto. Le superfici umane del cyberware sulle braccia vengono nascoste insieme alla pelle originale.

FBX e BLEND sono letti tramite Blender locale con esecuzione automatica degli script disattivata. Sono mantenute le mesh del corpo, testa e occhi, escludendo i vestiti riconosciuti; viene usata la forma neutra. I file senza scheletro sono disponibili solo per anteprima. I file Mayu `MISC/SP ... .fbx` esaminati non contengono uno scheletro: non possono animare V da soli. La lettura del `.blend` Mayu identifica lo scheletro ma non fornisce texture incorporate; non è stata verificata una sua conversione giocabile.

I profili riconoscono nomi comuni Unity, Mixamo, Blender e il mapping umanoide dei VRM. Questo non garantisce il porting automatico di ogni modello. Per nomi non riconosciuti si può aggiungere `avatarbridge.json` alla sorgente con `name`, `category` e `bones` (nome originale → nome umanoide, per esempio `{"bones":{"Pelvis_Custom":"hips"}}`). Un GLB deve contenere solo la base, un rig e texture incorporate. Le `.asset` Unity binarie, OBJ e GLTF esterni richiedono un export adatto.

## File installati

- `archive/pc/mod/zz_avatarbridge.archive`
- `archive/pc/mod/zz_avatarbridge.archive.xl`
- `bin/x64/plugins/cyber_engine_tweaks/mods/AvatarBridge/init.lua`

Dipendenze: ArchiveXL, Codeware e Cyber Engine Tweaks. L'app ne controlla la presenza prima di installare. Le dipendenze del gioco già presenti non vengono aggiornate o sostituite dall'installatore.

Se esiste `ExoticsCO-ExoticV.archive`, viene conservato nel backup locale e disattivato, perché è un'altra sostituzione di V. Le mod degli NPC Exotics e l'archivio core restano installati. **Rimuovi mod** ripristina il file disattivato. I salvataggi non vengono modificati dall'installatore.

Gli export, i backup e i manifest si trovano in `%LOCALAPPDATA%/CyberpunkAvatarBridge`. Se un file installato viene modificato dopo l'installazione, la rimozione automatica si ferma per preservare le modifiche.

## Sviluppo

Prerequisiti: Windows x64, Python 3.12, .NET SDK/Desktop Runtime 10 e 7-Zip.

```powershell
.\setup.ps1 -WithWolvenKit -WithBlender
& "$env:LOCALAPPDATA\CyberpunkAvatarBridge\runtime\Scripts\python.exe" -m unittest discover -s tests -v
.\package-source.ps1
```

Il setup scarica solamente dipendenze e strumenti pubblici: NumPy, PyYAML, Pillow, gltfpack 1.3, WolvenKit CLI 9.1.0 e, con l'opzione dedicata, Blender 4.5.9. Il normale uso è locale. La UI contatta GitHub soltanto per gli aggiornamenti, senza inviare avatar o telemetria.

Il pacchetto `AvatarBridge-source.zip` contiene esclusivamente una lista esplicita di sorgenti, test e documentazione. Non include avatar, texture, risorse del gioco, mod Exotics o strumenti di terzi. Il codice originale è MIT; gli asset restano sotto le rispettive licenze. Nessuna repository remota è stata creata.

## Procedura tecnica e fonti

Il builder rimappa le articolazioni sul rig del gioco, adatta la posa, genera tangenti e importa il GLB in un contenitore `.mesh` originale. Le mesh compilate vengono riesportate e confrontate per numero di triangoli e ossa. Gli occhi e le altre parti hanno materiali locali che referenziano le XBM dell'avatar. Le ossa di occhi e mandibola unite a Head conservano gli offset della forma neutra. Ogni base usa un namespace distinto.

ArchiveXL sostituisce le superfici originali con componenti disabilitati senza mesh, conservando nome e ID. Le patch includono le apparenze del corpo e della testa, oltre alle superfici legacy `torso`, `legs` e `shoes`. Il selettore CET abilita un solo avatar per volta e aggiorna i puppet dei menu. Le geometrie dei capi del gioco restano separate: alcuni richiedono un refit per vestire il nuovo corpo.

- [WolvenKit: importazione mesh e requisiti](https://wiki.redmodding.org/wolvenkit/wolvenkit-app/usage/import-export/models)
- [ArchiveXL: patch delle entità e degli scope](https://wiki.redmodding.org/cyberpunk-2077-modding/for-mod-creators-theory/core-mods-explained/archivexl/archivexl-resource-patching)
- [Scope ufficiali di V](https://github.com/psiberx/cp2077-archive-xl/blob/main/bundle/source/resources/PlayerBaseScope.xl)
- [Codeware: accesso ai componenti](https://github.com/psiberx/cp2077-codeware/blob/main/scripts/Entity/Entity.reds)
- [Biosculpted Exotics, riferimento funzionale](https://www.nexusmods.com/cyberpunk2077/mods/9797)
- [Refit: deformazioni e Garment Support](https://wiki.redmodding.org/cyberpunk-2077-modding/modding-guides/items-equipment/recolours-and-refits/r-and-r-troubleshooting)

La copertura3 conserva la pelle delle sezioni parzialmente scoperte: richiede almeno99,5% di area coperta, componenti abilitati e tutti i chunk della mesh attivi. Un capo con maniche tagliate o parti disattivate non applica la copertura della mesh completa. Le maschere uint64 vengono lette senza convertirle in numeri Lua in virgola mobile. L'anteprima usa i propri oggetti equipaggiati, con ripiego sul giocatore soltanto se non ne possiede. I file locali `coverage-components-player.json` e `coverage-components-paperdoll.json` permettono di controllare componenti e maschere. Le aperture particolari e gli attraversamenti del pelo richiedono comunque una prova nel gioco.

La copertura4 aggiunge una verifica delle gambe completamente racchiuse nei pantaloni: undici sezioni e192raggi per sezione devono incontrare il tessuto reale. Non viene usato un involucro convesso per riempire aperture. Bordi aperti, tagli e orli che attraversano la zona impediscono di nascondere quella parte della pelle; agli orli si conserva una sovrapposizione di8mm. Il controllo non applica questa regola a busto o giacche aperte. La prova dei triangoli non rileva aperture create soltanto dalla trasparenza della texture.

Il calcolo avviene durante la compilazione locale. Nel gioco si applicano le maschere già calcolate, senza eseguire questi raggi a ogni fotogramma.

- [RED4ext SDK: stato nativo isEnabled dei componenti](https://github.com/WopsS/RED4ext.SDK/blob/master/include/RED4ext/Scripting/Natives/entIComponent.hpp)

Il vincolo Garment Support interpola anche i raggi posteriori mancanti fra sezioni anatomiche valide, per conservare la separazione dalla schiena nelle forme compresse. Non estrapola oltre gli orli.

Le cuciture dei vestiti vengono riallineate anche fra submesh: i vertici coincidenti nella sorgente condividono posizione, pesi e stati Garment Support dopo il refit. UV e triangoli restano separati e conservati. Questo evita aperture tra busto e maniche quando i rispettivi campi di adattamento differiscono.
