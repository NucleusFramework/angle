// Smoke test for the trimmed ANGLE build.
//
// It walks the exact path the Nucleus Tao Windows backend takes
// (nucleus_tao_gl.c): an ANGLE D3D11 platform display, hardware device type
// first and WARP as the fallback, a pbuffer surface and an ES3 context. CI
// runners have no GPU, so in practice this exercises WARP -- which is also the
// path that matters most, since it is what RDP sessions, VMs and driverless
// machines get.
//
// It also asserts the two EGL extensions Nucleus needs beyond core EGL:
// EGL_EXT_device_query (to reach ANGLE's D3D11 device -- a client extension,
// queried against EGL_NO_DISPLAY) and EGL_ANGLE_d3d_texture_client_buffer
// (TextureView's zero-copy import, a display extension).

#define EGL_EGLEXT_PROTOTYPES 1
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

void fail(const char *message) {
    std::fprintf(stderr, "FAIL: %s (EGL error 0x%04x)\n", message, eglGetError());
    std::exit(1);
}

bool hasExtension(const char *extensions, const char *name) {
    if (!extensions) {
        return false;
    }
    const size_t length = std::strlen(name);
    for (const char *cursor = extensions; (cursor = std::strstr(cursor, name)) != nullptr;) {
        const bool leftOk = cursor == extensions || cursor[-1] == ' ';
        const bool rightOk = cursor[length] == ' ' || cursor[length] == '\0';
        if (leftOk && rightOk) {
            return true;
        }
        cursor += length;
    }
    return false;
}

EGLDisplay openDisplay(EGLint deviceType) {
    const EGLint attribs[] = {
        EGL_PLATFORM_ANGLE_TYPE_ANGLE,        EGL_PLATFORM_ANGLE_TYPE_D3D11_ANGLE,
        EGL_PLATFORM_ANGLE_DEVICE_TYPE_ANGLE, deviceType,
        EGL_NONE,
    };
    return eglGetPlatformDisplayEXT(EGL_PLATFORM_ANGLE_ANGLE, EGL_DEFAULT_DISPLAY, attribs);
}

}  // namespace

int main() {
    const char *clientExtensions = eglQueryString(EGL_NO_DISPLAY, EGL_EXTENSIONS);
    const char *requiredClient[] = {"EGL_ANGLE_platform_angle_d3d", "EGL_EXT_device_query"};
    for (const char *name : requiredClient) {
        if (!hasExtension(clientExtensions, name)) {
            std::fprintf(stderr, "FAIL: %s is missing\n", name);
            std::fprintf(stderr, "client extensions: %s\n",
                         clientExtensions ? clientExtensions : "(none)");
            return 1;
        }
        std::printf("%s: present\n", name);
    }

    const EGLint deviceTypes[] = {
        EGL_PLATFORM_ANGLE_DEVICE_TYPE_HARDWARE_ANGLE,
        EGL_PLATFORM_ANGLE_DEVICE_TYPE_D3D_WARP_ANGLE,
    };
    const char *deviceNames[] = {"hardware", "WARP"};

    EGLDisplay display = EGL_NO_DISPLAY;
    EGLint major = 0;
    EGLint minor = 0;
    const char *deviceName = nullptr;
    for (int i = 0; i < 2; ++i) {
        EGLDisplay candidate = openDisplay(deviceTypes[i]);
        if (candidate != EGL_NO_DISPLAY && eglInitialize(candidate, &major, &minor)) {
            display = candidate;
            deviceName = deviceNames[i];
            break;
        }
        std::printf("D3D11 %s device unavailable, trying the next one\n", deviceNames[i]);
    }
    if (display == EGL_NO_DISPLAY) {
        fail("no D3D11 display could be initialized");
    }
    std::printf("EGL %d.%d on the D3D11 %s device\n", major, minor, deviceName);

    const char *displayExtensions = eglQueryString(display, EGL_EXTENSIONS);
    const char *requiredDisplay[] = {"EGL_ANGLE_d3d_texture_client_buffer",
                                     "EGL_ANGLE_image_d3d11_texture"};
    for (const char *name : requiredDisplay) {
        if (!hasExtension(displayExtensions, name)) {
            std::fprintf(stderr, "FAIL: %s is missing\n", name);
            std::fprintf(stderr, "display extensions: %s\n", displayExtensions ? displayExtensions : "(none)");
            return 1;
        }
        std::printf("%s: present\n", name);
    }

    if (!eglBindAPI(EGL_OPENGL_ES_API)) {
        fail("eglBindAPI(EGL_OPENGL_ES_API)");
    }

    const EGLint configAttribs[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
        EGL_RED_SIZE,     8,               EGL_GREEN_SIZE,      8,
        EGL_BLUE_SIZE,    8,               EGL_ALPHA_SIZE,      8,
        EGL_NONE,
    };
    EGLConfig config = nullptr;
    EGLint configCount = 0;
    if (!eglChooseConfig(display, configAttribs, &config, 1, &configCount) || configCount == 0) {
        fail("eglChooseConfig found no ES3 pbuffer config");
    }

    const EGLint surfaceAttribs[] = {EGL_WIDTH, 64, EGL_HEIGHT, 64, EGL_NONE};
    EGLSurface surface = eglCreatePbufferSurface(display, config, surfaceAttribs);
    if (surface == EGL_NO_SURFACE) {
        fail("eglCreatePbufferSurface");
    }

    const EGLint contextAttribs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttribs);
    if (context == EGL_NO_CONTEXT) {
        fail("eglCreateContext");
    }
    if (!eglMakeCurrent(display, surface, surface, context)) {
        fail("eglMakeCurrent");
    }

    std::printf("GL_VENDOR:   %s\n", glGetString(GL_VENDOR));
    std::printf("GL_RENDERER: %s\n", glGetString(GL_RENDERER));
    std::printf("GL_VERSION:  %s\n", glGetString(GL_VERSION));

    glClearColor(0.0f, 0.5f, 1.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glFinish();

    unsigned char pixel[4] = {0, 0, 0, 0};
    glReadPixels(32, 32, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
    const GLenum error = glGetError();
    if (error != GL_NO_ERROR) {
        std::fprintf(stderr, "FAIL: GL error 0x%04x after clear/readback\n", error);
        return 1;
    }
    // WARP and hardware disagree on rounding, so the check stays coarse.
    if (pixel[0] > 8 || pixel[1] < 100 || pixel[1] > 160 || pixel[2] < 240 || pixel[3] < 240) {
        std::fprintf(stderr, "FAIL: read back (%u, %u, %u, %u), expected roughly (0, 128, 255, 255)\n",
                     pixel[0], pixel[1], pixel[2], pixel[3]);
        return 1;
    }
    std::printf("Rendered and read back (%u, %u, %u, %u)\n", pixel[0], pixel[1], pixel[2], pixel[3]);

    eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(display, context);
    eglDestroySurface(display, surface);
    eglTerminate(display);

    std::printf("PASS\n");
    return 0;
}
