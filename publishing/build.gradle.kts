// Publishes the trimmed ANGLE runtime DLLs as dev.nucleusframework:nucleus.angle-natives.
//
// The version is the Chromium branch number the DLLs were built from (8037 =
// what Chrome 154 stable shipped), so it moves on ANGLE's cadence rather than
// Nucleus'. A merge-back rebuild of the same branch becomes 8037.2, which
// orders after 8037 for both Gradle and Maven.
//
// The workflow stages the archives into src/main/resources, in the layout
// NativeLibraryLoader looks for, so Nucleus consumes this by declaring the
// dependency and nothing else: Class.getResource resolves through the
// classloader, which sees every jar on the classpath.
//
//     nucleus/native/win32-x64/{libEGL,libGLESv2}.dll
//     nucleus/native/win32-aarch64/{libEGL,libGLESv2}.dll
//
// There is no Java here. `java-library` is applied because it is what the
// publishing plugin configures a jar/sources/javadoc/POM set from; the sources
// and javadoc jars come out empty, which is what Maven Central asks for when
// there is nothing to put in them.

plugins {
    `java-library`
    alias(libs.plugins.vanniktechMavenPublish)
}

// Set by the workflow from the release tag: -PangleVersion=8037
val angleVersion: String = providers.gradleProperty("angleVersion").get()
val angleCommit: String = providers.gradleProperty("angleCommit").getOrElse("")

version = angleVersion
group = "dev.nucleusframework"

tasks.jar {
    manifest {
        attributes(
            "Implementation-Title" to "ANGLE runtime (Direct3D 11) for Nucleus",
            "Implementation-Version" to angleVersion,
            "Angle-Commit" to angleCommit,
        )
    }
}

mavenPublishing {
    coordinates("dev.nucleusframework", "nucleus.angle-natives", angleVersion)

    pom {
        name.set("Nucleus ANGLE Natives")
        description.set(
            "ANGLE runtime libraries (libEGL, libGLESv2) for Windows x64 and arm64, built for " +
                "Direct3D 11 only. Redistribution of the upstream ANGLE project, unmodified in " +
                "behaviour: the Vulkan, desktop-GL/WGL, SwiftShader, WebGPU and OpenCL backends " +
                "are disabled at build configuration level. Built by " +
                "https://github.com/NucleusFramework/angle, whose chromium/$angleVersion branch " +
                "is an untouched mirror of the upstream sources.",
        )
        url.set("https://github.com/NucleusFramework/angle")

        licenses {
            license {
                // ANGLE's licence, not Nucleus'. These are Google's binaries.
                name.set("BSD 3-Clause License")
                url.set("https://chromium.googlesource.com/angle/angle/+/main/LICENSE")
            }
        }

        developers {
            developer {
                id.set("nucleusframework")
                name.set("NucleusFramework")
                url.set("https://github.com/NucleusFramework")
            }
        }

        scm {
            url.set("https://github.com/NucleusFramework/angle")
            connection.set("scm:git:git://github.com/NucleusFramework/angle.git")
            developerConnection.set("scm:git:ssh://git@github.com/NucleusFramework/angle.git")
        }
    }

    publishToMavenCentral()
    if (project.hasProperty("signingInMemoryKey")) {
        signAllPublications()
    }
}
