using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;
namespace AvatarBridge;

static class ModelReader
{
    public static Model3DGroup Load(string file)
    {
        var bytes = File.ReadAllBytes(file);
        int jsonLen = BitConverter.ToInt32(bytes, 12);
        using var document = JsonDocument.Parse(bytes.AsMemory(20, jsonLen));
        var doc = document.RootElement;
        int binaryStart = 20 + jsonLen + 8;
        var result = new Model3DGroup();
        var materials = new List<Material>();
        if (doc.TryGetProperty("materials", out var mats))
        foreach (var mat in mats.EnumerateArray())
        {
            Brush brush = new SolidColorBrush(Color.FromRgb(174, 192, 166));
            if (mat.TryGetProperty("pbrMetallicRoughness", out var pbr) && pbr.TryGetProperty("baseColorTexture", out var tex))
            {
                var texture = doc.GetProperty("textures")[tex.GetProperty("index").GetInt32()];
                var image = doc.GetProperty("images")[texture.GetProperty("source").GetInt32()];
                var view = doc.GetProperty("bufferViews")[image.GetProperty("bufferView").GetInt32()];
                using var stream = new MemoryStream(bytes, binaryStart + Offset(view), view.GetProperty("byteLength").GetInt32());
                var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad; bitmap.StreamSource = stream; bitmap.EndInit(); bitmap.Freeze();
                brush = new ImageBrush(bitmap) { ViewportUnits = BrushMappingMode.Absolute, Viewport = new Rect(0, 0, 1, 1), TileMode = TileMode.Tile };
            }
            brush.Freeze(); var material = new DiffuseMaterial(brush); material.Freeze(); materials.Add(material);
        }
        var nodes = doc.GetProperty("nodes");
        void Walk(int nodeIndex, Matrix3D parent)
        {
            var node = nodes[nodeIndex];
            Matrix3D local = Matrix3D.Identity;
            if (node.TryGetProperty("matrix", out var matrix))
            {
                var m = matrix.EnumerateArray().Select(v => v.GetDouble()).ToArray();
                local = new Matrix3D(m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8], m[9], m[10], m[11], m[12], m[13], m[14], m[15]);
            }
            else
            {
                if (node.TryGetProperty("scale", out var s)) local.Scale(new Vector3D(s[0].GetDouble(), s[1].GetDouble(), s[2].GetDouble()));
                if (node.TryGetProperty("rotation", out var q)) local.Rotate(new Quaternion(q[0].GetDouble(), q[1].GetDouble(), q[2].GetDouble(), q[3].GetDouble()));
                if (node.TryGetProperty("translation", out var t)) local.Translate(new Vector3D(t[0].GetDouble(), t[1].GetDouble(), t[2].GetDouble()));
            }
            local.Append(parent);
            if (node.TryGetProperty("mesh", out var meshIndex))
            foreach (var primitive in doc.GetProperty("meshes")[meshIndex.GetInt32()].GetProperty("primitives").EnumerateArray())
            {
                var attributes = primitive.GetProperty("attributes");
                var positions = Read(attributes.GetProperty("POSITION").GetInt32(), 3);
                var geometry = new MeshGeometry3D();
                for (int i = 0; i < positions.Length; i += 3) geometry.Positions.Add(local.Transform(new Point3D(positions[i], positions[i + 1], positions[i + 2])));
                if (attributes.TryGetProperty("TEXCOORD_0", out var uvIndex))
                {
                    var uv = Read(uvIndex.GetInt32(), 2);
                    for (int i = 0; i < uv.Length; i += 2) geometry.TextureCoordinates.Add(new Point(uv[i], uv[i + 1]));
                }
                if (primitive.TryGetProperty("indices", out var idx))
                    foreach (var value in Read(idx.GetInt32(), 1)) geometry.TriangleIndices.Add((int)value);
                else for (int i = 0; i < geometry.Positions.Count; i++) geometry.TriangleIndices.Add(i);
                var material = primitive.TryGetProperty("material", out var mi) && mi.GetInt32() < materials.Count ? materials[mi.GetInt32()] : new DiffuseMaterial(Brushes.LightGray);
                geometry.Freeze(); var model = new GeometryModel3D(geometry, material) { BackMaterial = material }; model.Freeze(); result.Children.Add(model);
            }
            if (node.TryGetProperty("children", out var children)) foreach (var child in children.EnumerateArray()) Walk(child.GetInt32(), local);
        }
        double[] Read(int accessorIndex, int components)
        {
            var accessor = doc.GetProperty("accessors")[accessorIndex];
            if (accessor.TryGetProperty("sparse", out _)) throw new InvalidOperationException("Questa anteprima richiede un GLB senza accessori sparse.");
            var view = doc.GetProperty("bufferViews")[accessor.GetProperty("bufferView").GetInt32()];
            int type = accessor.GetProperty("componentType").GetInt32();
            int size = type == 5126 || type == 5125 ? 4 : type == 5123 ? 2 : 1;
            int count = accessor.GetProperty("count").GetInt32();
            int stride = view.TryGetProperty("byteStride", out var st) ? st.GetInt32() : size * components;
            int start = binaryStart + Offset(view) + Offset(accessor);
            var values = new double[count * components];
            for (int i = 0; i < count; i++) for (int j = 0; j < components; j++)
            {
                int p = start + i * stride + j * size;
                values[i * components + j] = type switch { 5126 => BitConverter.ToSingle(bytes, p), 5125 => BitConverter.ToUInt32(bytes, p), 5123 => BitConverter.ToUInt16(bytes, p), 5121 => bytes[p], _ => throw new InvalidOperationException("Formato GLB non supportato dall'anteprima.") };
            }
            return values;
        }
        var scene = doc.GetProperty("scenes")[doc.TryGetProperty("scene", out var sc) ? sc.GetInt32() : 0];
        foreach (var n in scene.GetProperty("nodes").EnumerateArray()) Walk(n.GetInt32(), Matrix3D.Identity);
        result.Freeze(); return result;
    }
    static int Offset(JsonElement value) => value.TryGetProperty("byteOffset", out var o) ? o.GetInt32() : 0;
}
