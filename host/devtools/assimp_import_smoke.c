/* Test-phase native Assimp C API smoke: parse a real OBJ using installed dylib. */
#include <assimp/cimport.h>
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc != 2) {
        fputs("usage: assimp-import-smoke <model.obj>\n", stderr);
        return 2;
    }
    const struct aiScene *scene = aiImportFile(argv[1], aiProcess_Triangulate);
    if (scene == NULL) {
        fprintf(stderr, "Assimp model import failed: %s\n", aiGetErrorString());
        return 1;
    }
    int success = (scene->mNumMeshes == 1 && scene->mMeshes[0]->mNumFaces == 1);
    printf("Assimp imported meshes=%u faces=%u\n", scene->mNumMeshes,
           scene->mMeshes[0]->mNumFaces);
    aiReleaseImport(scene);
    return success ? 0 : 1;
}
