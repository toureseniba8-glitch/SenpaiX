import os

# Le code HTML mis à jour avec le bouton de pièce jointe
form_code = """
    <!-- Formulaire d'envoi avec support multimédia -->
    <form action="" method="POST" enctype="multipart/form-data" class="flex gap-2 items-center bg-gray-800 p-2 rounded-xl mt-4">
        <input type="text" name="content" placeholder="Écris au groupe..." class="flex-1 bg-transparent text-white outline-none px-2">
        
        <!-- Bouton Pièce jointe -->
        <label class="cursor-pointer text-gray-400 hover:text-white px-2 text-xl" title="Ajouter une image ou vidéo">
            📎 <input type="file" name="media" accept="image/*,video/*" class="hidden">
        </label>
        
        <button type="submit" class="bg-orange-500 text-white px-4 py-2 rounded-lg font-bold">Envoyer</button>
    </form>
"""

# Parcourt tous les fichiers HTML pour s'assurer que le formulaire y est
for root, dirs, files in os.walk("templates"):
    for file in files:
        if "group" in file.lower() or "chat" in file.lower():
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if "enctype=\"multipart/form-data\"" not in content:
                print(f"[INFO] Mise à jour du template : {file}")
                # On peut injecter le formulaire ou s'assurer qu'il est présent

