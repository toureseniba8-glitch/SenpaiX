import os
import subprocess

# 1. Mise à jour du template HTML (group.html) avec le bouton de pièce jointe (📎)
html_content = """{% extends "base.html" %}
{% block content %}
<div class="max-w-4xl mx-auto p-4 text-white">
    <h1 class="text-2xl font-bold mb-4 text-orange-400">{{ group.name }}</h1>
    
    <!-- Section repliable pour les informations -->
    <details class="bg-gray-800 p-4 rounded-xl mb-4 shadow-md">
        <summary class="font-bold cursor-pointer text-orange-400">📋 Informations et règles du groupe</summary>
        <p class="mt-2 text-sm text-gray-300">{{ group.description }}</p>
    </details>

    <!-- Messages -->
    <div class="space-y-4 mb-4">
        {% for msg in messages %}
        <div class="bg-gray-900 p-3 rounded-lg">
            <span class="text-xs text-orange-400 font-bold">{{ msg.user.pseudo }}</span>
            <p class="mt-1">{{ msg.content }}</p>
            {% if msg.media_url %}
                <div class="mt-2">
                    {% if msg.media_url.endswith(('mp4', 'webm', 'ogg')) %}
                        <video controls class="max-h-60 rounded-lg w-full">
                            <source src="{{ msg.media_url }}" type="video/mp4">
                        </video>
                    {% else %}
                        <img src="{{ msg.media_url }}" alt="Média" class="max-h-60 rounded-lg object-cover">
                    {% endif %}
                </div>
            {% endif %}
        </div>
        {% endfor %}
    </div>

    <!-- Formulaire d'envoi avec support multimédia -->
    <form action="{{ url_for('send_group_message', group_id=group.id) }}" method="POST" enctype="multipart/form-data" class="flex gap-2 items-center bg-gray-800 p-2 rounded-xl">
        <input type="text" name="content" placeholder="Écris au groupe..." class="flex-1 bg-transparent text-white outline-none px-2">
        
        <!-- Bouton Pièce jointe -->
        <label class="cursor-pointer text-gray-400 hover:text-white px-2 text-xl" title="Ajouter une image ou vidéo">
            📎 <input type="file" name="media" accept="image/*,video/*" class="hidden">
        </label>
        
        <button type="submit" class="bg-orange-500 text-white px-4 py-2 rounded-lg font-bold">Envoyer</button>
    </form>
</div>
{% endblock %}
"""

os.makedirs('templates', exist_ok=True)
with open('templates/group.html', 'w', encoding='utf-8') as f:
    f.write(html_content)
print("[INFO] Template HTML mis à jour avec succès.")


# 2. Vérification et injection de la logique Cloudinary dans app.py si nécessaire
app_code_snippet = """
import cloudinary.uploader

@app.route('/group/<int:group_id>/send', methods=['POST'])
def send_group_message(group_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    content = request.form.get('content')
    file = request.files.get('media')
    media_url = None

    if file and file.filename != '':
        upload_result = cloudinary.uploader.upload(file, resource_type="auto")
        media_url = upload_result.get('secure_url')

    new_message = GroupMessage(
        content=content,
        media_url=media_url,
        user_id=session['user_id'],
        group_id=group_id
    )
    db.session.add(new_message)
    db.session.commit()
    
    return redirect(url_for('view_group', group_id=group_id))
"""

# On vérifie si la route d'envoi existe déjà dans app.py, sinon on l'ajoute proprement
if os.path.exists('app.py'):
    with open('app.py', 'r', encoding='utf-8') as f:
        app_content = f.read()
    
    if 'def send_group_message' not in app_content:
        with open('app.py', 'a', encoding='utf-8') as f:
            f.write(app_code_snippet)
        print("[INFO] Route Cloudinary ajoutée à app.py.")
    else:
        print("[INFO] La route send_group_message est déjà présente dans app.py.")

# 3. Automatisation des commandes Git (add, commit, push)
print("[INFO] Lancement de la synchronisation Git...")
subprocess.run(["git", "add", "."])
subprocess.run(["git", "commit", "-m", "Mise a jour automatique groupes multimedia et repliables"])
result = subprocess.run(["git", "push", "origin", "main"])

if result.returncode == 0:
    print("[SUCCÈS] Tout a été déployé et poussé vers GitHub/Render avec succès !")
else:
    print("[ERREUR] Un problème est survenu lors du git push.")

